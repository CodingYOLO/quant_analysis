"""
Notifier 抽象接口 + Server酱/邮箱实现。
通过 get_notifier() 工厂方法按配置返回实例。
"""

import logging
import smtplib
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


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


class EmailNotifier(Notifier):
    """通过 SMTP 发送邮件。"""

    def __init__(self, host: str, port: int, user: str, password: str, to: str) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._to = to

    def send(self, title: str, content: str) -> bool:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = title
            msg["From"] = self._user
            msg["To"] = self._to
            msg.attach(MIMEText(content, "plain", "utf-8"))

            with smtplib.SMTP(self._host, self._port) as smtp:
                smtp.starttls()
                smtp.login(self._user, self._password)
                smtp.sendmail(self._user, [self._to], msg.as_string())

            logger.info("邮件推送成功 -> %s", self._to)
            return True
        except Exception as e:
            logger.error("邮件推送异常: %s", e)
            return False


class NoopNotifier(Notifier):
    """空实现，用于禁用推送（调试模式）。"""

    def send(self, title: str, content: str) -> bool:
        logger.info("推送渠道为 none，跳过通知: %s", title)
        return True


def get_notifier() -> Notifier:
    """工厂方法：根据配置返回对应的 Notifier 实例。"""
    settings = get_settings()
    channel = settings.notify_channel.lower()

    if channel == "serverchan":
        return ServerChanNotifier(send_key=settings.serverchan_send_key)
    elif channel == "email":
        return EmailNotifier(
            host=settings.email_smtp_host,
            port=settings.email_smtp_port,
            user=settings.email_user,
            password=settings.email_password,
            to=settings.email_to,
        )
    elif channel == "none":
        return NoopNotifier()
    else:
        logger.warning("未知推送渠道 '%s'，使用空实现", channel)
        return NoopNotifier()
