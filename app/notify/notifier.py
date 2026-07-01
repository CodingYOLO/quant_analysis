"""
Notifier 抽象接口 + Server酱/邮箱实现。
通过 get_notifier() 工厂方法按配置返回实例。

支持渠道：
  - serverchan  : Server酱微信推送
  - email       : SMTP邮件（支持163/Gmail等）
  - both        : Server酱 + 邮件同时发送
  - none        : 禁用（调试用）

163邮箱 SMTP 配置：
  EMAIL_SMTP_HOST=smtp.163.com
  EMAIL_SMTP_PORT=465
  EMAIL_USE_SSL=true
  EMAIL_USER=yourname@163.com
  EMAIL_PASSWORD=（163授权码，不是登录密码）
  EMAIL_TO=接收邮箱地址
"""

import logging
import smtplib
import ssl
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def _normalize_md_tables(md_text: str) -> str:
    """
    Python-Markdown 的 tables / 列表扩展都要求块前有空行，否则整段当普通文本。
    LLM 常把"标题行"和紧跟的表格/列表粘在一起，这里自动补空行。

    处理两类块：
      - 表格：行以 | 开头，且下一行是分隔行（|---|---|）
      - 列表：行以 "- " / "* " / "1. " 开头
    规则：若该块首行前一行非空、且不属于同类块，则插入一个空行。
    """
    import re as _re

    lines = md_text.split("\n")
    result: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()

        is_table_header = (
            stripped.startswith("|")
            and i + 1 < len(lines)
            and set(lines[i + 1].strip()) <= set("|-: ")
            and "-" in lines[i + 1]
        )
        is_list_item = bool(_re.match(r"^([-*]\s|\d+\.\s)", stripped))

        prev = result[-1].strip() if result else ""
        prev_is_table = prev.startswith("|")
        prev_is_list = bool(_re.match(r"^([-*]\s|\d+\.\s)", prev))

        # 表头前补空行
        if is_table_header and prev and not prev_is_table:
            result.append("")
        # 列表首项前补空行（前一行非空、且不是列表项）
        elif is_list_item and prev and not prev_is_list:
            result.append("")

        result.append(line)
    return "\n".join(result)


def _md_to_html_fragment(md_text: str) -> str:
    """Markdown → HTML 正文片段（无 <style>/<html>/footer）。

    供网页深色主题自行套样式用——避免邮件白底内联样式泄漏到深色页面
    （否则 td/p 继承 #222 深色字、tr:nth-child(even) 套白底，深色页上半数文字隐形）。
    """
    md_text = _normalize_md_tables(md_text)
    try:
        import markdown as md_lib
        return md_lib.markdown(md_text, extensions=["tables", "fenced_code"])
    except ImportError:
        return md_text.replace("\n", "<br>")        # 降级：简单替换换行


def _md_to_html(md_text: str) -> str:
    """
    将 Markdown 转为带样式的完整 HTML 文档，适合邮件客户端展示（白底）。
    依赖 markdown 库（pip install markdown）。
    """
    body = _md_to_html_fragment(md_text)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ font-family: -apple-system, 'PingFang SC', Arial, sans-serif;
         font-size: 16px; line-height: 1.7; color: #222;
         max-width: 780px; margin: 0 auto; padding: 20px; }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #1a73e8; padding-bottom: 8px; color: #1a73e8; }}
  h2 {{ font-size: 19px; margin-top: 28px; color: #333; border-left: 4px solid #1a73e8; padding-left: 10px; }}
  h3 {{ font-size: 17px; color: #444; margin-top: 18px; }}
  p {{ font-size: 16px; margin: 10px 0; line-height: 1.8; }}
  li {{ font-size: 16px; margin: 8px 0; line-height: 1.7; }}
  table {{ border-collapse: collapse; width: 100%; margin: 14px 0; }}
  th {{ background: #1a73e8; color: #fff; padding: 10px 14px; text-align: left; font-size: 15px; }}
  td {{ padding: 9px 14px; border-bottom: 1px solid #eee; font-size: 15px; }}
  tr:nth-child(even) {{ background: #f8f9fa; }}
  blockquote {{ border-left: 3px solid #ccc; margin: 10px 0; padding: 6px 14px;
               color: #666; background: #f9f9f9; font-size: 15px; }}
  code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-size: 14px; }}
  pre {{ background: #f5f5f5; padding: 14px; border-radius: 6px; overflow-x: auto; font-size: 14px; }}
  .footer {{ margin-top: 36px; padding-top: 12px; border-top: 1px solid #eee;
             font-size: 13px; color: #999; }}
</style>
</head>
<body>
{body}
<div class="footer">⚠️ 本报告由 A股Agent 自动生成，仅供参考，不构成投资建议。</div>
</body>
</html>"""


class Notifier(ABC):
    """推送通知抽象基类。"""

    @abstractmethod
    def send(self, title: str, content: str) -> bool:
        """
        发送通知。

        Args:
            title: 通知标题
            content: 正文（支持 Markdown）

        Returns:
            True 表示发送成功
        """


class ServerChanNotifier(Notifier):
    """通过 Server酱 推送到个人微信。"""

    _API_URL = "https://sctapi.ftqq.com/{send_key}.send"

    def __init__(self, send_key: str) -> None:
        if not send_key:
            raise ValueError("Server酱 SendKey 不能为空，请在 .env 中配置 SERVERCHAN_SEND_KEY")
        self._send_key = send_key

    def send(self, title: str, content: str) -> bool:
        url = self._API_URL.format(send_key=self._send_key)
        try:
            resp = httpx.post(
                url,
                data={"title": title[:32], "desp": content},
                timeout=15.0,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 0:
                logger.info("Server酱推送成功")
                return True
            else:
                logger.warning("Server酱推送失败: %s", result)
                return False
        except Exception as e:
            logger.error("Server酱推送异常: %s", e)
            return False


def all_device_keys() -> str:
    """全设备 key（用户1+用户2，逗号拼接）——全市场信号(大盘/板块/急拉/快照)全量同步两台都收。"""
    from app.config import get_settings
    s = get_settings()
    return ",".join(x for x in (s.bark_key, s.bark_key_user2) if x and x.strip())


def owner_device_keys(owners) -> str:
    """给定归属人集合 → 对应设备 key（逗号拼接）。自选/持仓个性化信号按人路由用。

    owners 取值见 db.WATCH_OWNERS：me=用户1(我) / dad=用户2(爸爸)。某人未配设备则其份额为空。
    """
    from app.config import get_settings
    s = get_settings()
    m = {"me": s.bark_key, "dad": s.bark_key_user2}
    return ",".join(m[o] for o in owners if m.get(o) and m[o].strip())


def _bark_keys(raw: str) -> list[str]:
    """逗号分隔的多设备 key → 去空去重列表（全量同步到每台手机用·保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for k in (raw or "").split(","):
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _bark_post_one(key: str, payload: dict) -> bool:
    """向单台设备 POST。api.day.app 在境内偶发 SSL/握手超时→**重试2次**(短超时·别把扫描线程堵久)。
    失败只记日志(key 尾4位脱敏)不抛——一台失败不影响其余设备。"""
    last = ""
    for attempt in range(3):                          # 最多3次·应对 api.day.app 境内间歇性超时
        try:
            resp = httpx.post(f"https://api.day.app/{key}", json=payload, timeout=6.0)
            resp.raise_for_status()
            ok = resp.json().get("code") == 200
            if not ok:
                logger.warning("Bark推送失败(设备…%s): %s", key[-4:], resp.text[:120])
            return ok
        except Exception as e:
            last = str(e)
    logger.error("Bark推送异常(设备…%s·重试3次仍失败): %s", key[-4:], last)
    return False


def push_bark(title: str, body: str, *, key: str = "", group: str = "盯盘",
              url: str = "", sound: str = "", level: str = "") -> bool:
    """Bark(iOS) 实时推送（盯盘提醒用）。失败返回 False。

    **多设备全量同步**：key / `settings.bark_key` 支持**逗号分隔多个 key**，同一条消息逐台 POST，
    每台独立成败、互不影响；**至少一台成功即返回 True**（避免某台 key 失效拖累冷却/主设备语义）。
    单 key 写法完全向后兼容；key 留空则读 settings.bark_key。

    Bark POST API：POST https://api.day.app/{key}，JSON {title, body, group, isArchive, url, level}。
    level: timeSensitive(穿透勿扰) / active(正常) / passive(静默) — 信号重要度分级用。
    """
    from app.config import get_settings
    keys = _bark_keys(key or get_settings().bark_key)
    if not keys:
        return False
    payload = {"title": title[:40], "body": body, "group": group, "isArchive": 1}
    if url:
        payload["url"] = url
    if sound:
        payload["sound"] = sound
    if level:
        payload["level"] = level
    ok_any = False
    for k in keys:
        if _bark_post_one(k, payload):
            ok_any = True
    return ok_any


class EmailNotifier(Notifier):
    """
    通过 SMTP 发送 HTML 格式邮件。

    支持两种连接方式：
      - SSL (port=465)：163/QQ邮箱默认，直接建立加密连接
      - STARTTLS (port=587)：Gmail默认，先明文后升级加密
    自动根据端口号判断使用哪种方式。
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        to: str,
        use_ssl: bool | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._to = to
        # 默认：465端口用SSL，其余用STARTTLS
        self._use_ssl = use_ssl if use_ssl is not None else (port == 465)

    def send(self, title: str, content: str) -> bool:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = title
            msg["From"] = f"A股Agent <{self._user}>"
            msg["To"] = self._to

            # 纯文本备用（邮件客户端不支持HTML时显示，保留完整内容）
            msg.attach(MIMEText(content, "plain", "utf-8"))

            # HTML 主体放最后：RFC 2046 规定 multipart/alternative 优先选最后一个 part
            html = _md_to_html(content)
            msg.attach(MIMEText(html, "html", "utf-8"))

            if self._use_ssl:
                # 163/QQ：465端口，SSL直连
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(self._host, self._port, context=context) as smtp:
                    smtp.login(self._user, self._password)
                    smtp.sendmail(self._user, [self._to], msg.as_string())
            else:
                # Gmail：587端口，STARTTLS
                with smtplib.SMTP(self._host, self._port) as smtp:
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.login(self._user, self._password)
                    smtp.sendmail(self._user, [self._to], msg.as_string())

            logger.info("邮件发送成功 -> %s", self._to)
            return True
        except Exception as e:
            logger.error("邮件发送失败: %s", e)
            return False


class MultiNotifier(Notifier):
    """同时发送到多个渠道（如 Server酱 + 邮件）。"""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = notifiers

    def send(self, title: str, content: str) -> bool:
        results = [n.send(title, content) for n in self._notifiers]
        return any(results)  # 至少一个成功即视为成功


class NoopNotifier(Notifier):
    """空实现，用于禁用推送（调试模式）。"""

    def send(self, title: str, content: str) -> bool:
        logger.info("推送渠道为 none，跳过通知: %s", title)
        return True


def get_notifier() -> Notifier:
    """
    工厂方法：根据配置返回对应的 Notifier 实例。

    支持渠道（NOTIFY_CHANNEL）:
      serverchan  Server酱微信推送
      email       仅邮件
      both        Server酱 + 邮件同时发
      none        禁用
    """
    settings = get_settings()
    channel = settings.notify_channel.lower()

    def _make_email() -> EmailNotifier:
        return EmailNotifier(
            host=settings.email_smtp_host,
            port=settings.email_smtp_port,
            user=settings.email_user,
            password=settings.email_password,
            to=settings.email_to,
            use_ssl=settings.email_use_ssl,
        )

    if channel == "serverchan":
        return ServerChanNotifier(send_key=settings.serverchan_send_key)
    elif channel == "email":
        return _make_email()
    elif channel == "both":
        notifiers: list[Notifier] = []
        if settings.serverchan_send_key:
            notifiers.append(ServerChanNotifier(send_key=settings.serverchan_send_key))
        notifiers.append(_make_email())
        return MultiNotifier(notifiers)
    elif channel == "none":
        return NoopNotifier()
    else:
        logger.warning("未知推送渠道 '%s'，使用空实现", channel)
        return NoopNotifier()
