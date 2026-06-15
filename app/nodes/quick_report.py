"""
快讯报告生成器（Quick Report）。

支持三个时段，每个时段只分析对应时间窗口内的信息：
  - pre   盘前 (~9:00)：昨日15:00 → 今日9:00，隔夜消息+今日方向
  - mid   盘中半天 (~12:00)：09:00 → 12:00，上午催化+午后策略
  - post  盘后 (~16:05)：当日全天，复盘+明日布局

信息源（三路合并）：
  - [新闻] 财联社电报 / 东方财富财经快讯
  - [公告] akshare stock_notice_report（重大事项/资产重组/持股变动/风险提示）
  - [研报] akshare stock_research_report_em（当日观察池个股的近3日券商研报）

核心设计原则：
  - 所有信息来源必须标注 [新闻]/[公告]/[研报] 标签
  - LLM输出必须有明确的多空方向和操作逻辑
  - 报告精确到分钟，反映当时信息状态
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Literal

import pandas as pd

from app.config import get_settings
from app.data.akshare_provider import AkshareProvider

logger = logging.getLogger(__name__)

SessionType = Literal["pre", "mid", "post"]

# 每个时段的中文名称和新闻时间窗口（小时偏移，相对于当天0点）
_SESSION_META = {
    "pre":  {"label": "盘前",   "start_h": -9,  "end_h": 9},    # 昨日15:00到今日9:00
    "mid":  {"label": "盘中",   "start_h": 9,   "end_h": -1},   # 09:00 → 运行时刻（动态）
    "post": {"label": "盘后",   "start_h": 9,   "end_h": 17},
}


def build_quick_report(
    session: SessionType,
    trade_date: str | None = None,
) -> tuple[str, str, str]:
    """
    生成指定时段的快讯报告。

    Args:
        session: "pre" | "mid" | "post"
        trade_date: YYYYMMDD，默认今日

    Returns:
        (filepath, title, content) — Markdown 内容，已写入文件
    """
    settings = get_settings()
    now = datetime.datetime.now()
    today = trade_date or now.strftime("%Y%m%d")
    now_str = now.strftime("%Y-%m-%d %H:%M")
    meta = _SESSION_META[session]

    # ---- 拉取三路信息源 ----
    news_df = _fetch_news(today, meta["start_h"], meta["end_h"])
    notices_text = _fetch_notices(today)
    research_text = _fetch_research_reports(today)

    has_content = not news_df.empty or notices_text or research_text

    # ---- 无任何内容时直接返回提示，拒绝生成 ----
    if not has_content:
        content = (
            f"## ⚠️ {meta['label']}快讯暂无内容\n\n"
            f"> 当前时间窗口（{_window_desc(meta['start_h'], meta['end_h'])}）内"
            f"三路信息源（财联社/公告/研报）均无新内容。\n>\n"
            f"> 本系统拒绝在无真实信息来源的情况下生成分析，"
            f"以免干扰您的判断。\n\n"
            f"请稍后刷新或等待下一时段报告。"
        )
        filepath = settings.report_dir / f"{today}_{now.strftime('%H%M')}_{session}.md"
        filepath.write_text(f"# A股{meta['label']}快讯\n> 📅 **{now_str}**\n\n" + content, encoding="utf-8")
        logger.warning("[快讯] 三路信息源均为空，已生成空报告: %s", filepath)
        title = f"【{meta['label']}】{today[4:6]}/{today[6:]} | 暂无新消息"
        return str(filepath), title, content

    # ---- 调用 LLM 生成分析 ----
    content = _generate_analysis(
        session, meta["label"], now_str, news_df, today,
        notices_text=notices_text,
        research_text=research_text,
    )

    # ---- 保存文件 ----
    filename = f"{today}_{now.strftime('%H%M')}_{session}.md"
    filepath = settings.report_dir / filename
    news_count = len(news_df)
    header = (
        f"# A股{meta['label']}快讯\n"
        f"> 📅 **{now_str}**　|　新闻: {news_count} 条"
        f"　|　公告: {'有' if notices_text else '无'}"
        f"　|　研报: {'有' if research_text else '无'}\n"
        f"> ⚠️ LLM信息聚合，不构成投资建议\n\n"
    )
    filepath.write_text(header + content, encoding="utf-8")
    logger.info("[快讯] 报告已保存: %s", filepath)

    title = f"【{meta['label']}】{today[4:6]}/{today[6:]} | {_headline(content)}"
    return str(filepath), title, header + content


# --------------------------------------------------------------------------- #
# 内部实现
# --------------------------------------------------------------------------- #

def _fetch_notices(today: str) -> str:
    """
    拉取当日重大公告并格式化为文本块。
    返回空字符串表示当日无公告。
    """
    try:
        ak_provider = AkshareProvider()
        df = ak_provider.get_company_notices(today)
        if df is None or df.empty:
            return ""

        # 动态找列名
        code_col = next((c for c in df.columns if "代码" in c or "股票" in c), None)
        name_col = next((c for c in df.columns if "简称" in c or "名称" in c), None)
        title_col = next((c for c in df.columns if "标题" in c or "公告" in c), None)
        type_col = next((c for c in df.columns if "类型" in c or "种类" in c), None)

        lines = []
        for _, row in df.head(20).iterrows():
            parts = []
            if code_col:
                parts.append(str(row.get(code_col, "")).strip())
            if name_col:
                parts.append(str(row.get(name_col, "")).strip())
            if type_col:
                parts.append(f"[{str(row.get(type_col, '')).strip()}]")
            if title_col:
                parts.append(str(row.get(title_col, "")).strip()[:80])
            if parts:
                lines.append(" ".join(parts))

        return "\n".join(lines) if lines else ""
    except Exception as e:
        logger.warning("[快讯] 公告拉取失败: %s", e)
        return ""


def _fetch_research_reports(today: str) -> str:
    """
    拉取最近3天针对观察池个股的券商研报，格式化为文本块。
    观察池从 strategy.db 的 watchlist 读取，最多取前15只。
    返回空字符串表示无近期研报。
    """
    try:
        from app.strategy.forward_tracker import get_recent_watchlist_perf
        recent_list = get_recent_watchlist_perf(trade_date=today, days=5)
        if not recent_list:
            return ""

        # get_recent_watchlist_perf 返回 list[dict]，提取 ts_code 字段
        ts_codes = list({item["ts_code"] for item in recent_list if "ts_code" in item})[:15]
        if not ts_codes:
            return ""

        ak_provider = AkshareProvider()
        df = ak_provider.get_research_reports(ts_codes, max_days=3)
        if df is None or df.empty:
            return ""

        # 字段名以真实 akshare 返回为准：报告名称、东财评级、机构、日期
        lines = []
        for _, row in df.head(20).iterrows():
            code = str(row.get("_stock_code", row.get("股票代码", ""))).strip()
            name = str(row.get("股票简称", "")).strip()
            org = str(row.get("机构", "")).strip()
            rating = str(row.get("东财评级", "")).strip()
            report = str(row.get("报告名称", "")).strip()[:80]
            date = str(row.get("日期", ""))[:10]
            parts = [p for p in [code, name, f"({org})" if org else "", rating, report, date] if p]
            if parts:
                lines.append(" ".join(parts))

        return "\n".join(lines) if lines else ""
    except Exception as e:
        logger.warning("[快讯] 研报拉取失败: %s", e)
        return ""


def _fetch_news(today: str, start_h: int, end_h: int) -> pd.DataFrame:
    """
    拉取财联社新闻并按时间窗口过滤。
    start_h < 0 表示取昨日（如 -9 表示昨日 15:00）。
    """
    ak = AkshareProvider()
    frames = []

    # 财联社新闻（今日）
    try:
        df_cls = ak.get_cls_news(today)
        if not df_cls.empty:
            frames.append(df_cls)
    except Exception as e:
        logger.warning("拉取财联社今日新闻失败: %s", e)

    # 华尔街见闻快讯（今日）
    try:
        df_wscn = ak.get_wscn_lives(today)
        if not df_wscn.empty:
            frames.append(df_wscn)
            logger.info("华尔街见闻快讯: 获取 %d 条", len(df_wscn))
    except Exception as e:
        logger.warning("拉取华尔街见闻失败: %s", e)

    # 需要昨日新闻（盘前报告）
    if start_h < 0:
        yesterday = _offset_date(today, -1)
        try:
            df_yest = ak.get_cls_news(yesterday)
            if not df_yest.empty:
                frames.append(df_yest)
        except Exception as e:
            logger.warning("拉取财联社昨日新闻失败: %s", e)
        try:
            df_wscn_yest = ak.get_wscn_lives(yesterday)
            if not df_wscn_yest.empty:
                frames.append(df_wscn_yest)
        except Exception as e:
            logger.warning("拉取华尔街见闻昨日新闻失败: %s", e)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return df

    # 解析时间并过滤窗口
    time_col = "发布时间" if "发布时间" in df.columns else df.columns[1]
    df["_ts"] = pd.to_datetime(df[time_col], errors="coerce", utc=True).dt.tz_convert("Asia/Shanghai")

    tz_sh = datetime.timezone(datetime.timedelta(hours=8))
    today_dt = datetime.datetime.strptime(today, "%Y%m%d")
    if start_h >= 0:
        start_ts = today_dt.replace(hour=start_h, minute=0, tzinfo=tz_sh)
    else:
        # start_h 为负，表示前一天
        prev = today_dt - datetime.timedelta(days=1)
        start_ts = prev.replace(hour=24 + start_h, minute=0, tzinfo=tz_sh)

    # end_h == -1 为哨兵值，表示动态取当前时刻（mid 时段使用）
    if end_h == -1:
        now = datetime.datetime.now(tz=tz_sh)
        end_ts = now
    else:
        end_ts = today_dt.replace(hour=end_h, minute=59, tzinfo=tz_sh)

    mask = (df["_ts"] >= start_ts) & (df["_ts"] <= end_ts)
    filtered = df[mask].sort_values("_ts", ascending=False).reset_index(drop=True)
    return filtered


def _generate_analysis(
    session: SessionType,
    label: str,
    now_str: str,
    news_df: pd.DataFrame,
    today: str,
    *,
    notices_text: str = "",
    research_text: str = "",
) -> str:
    """调用 DeepSeek 生成 A股聚焦的时段分析，融合新闻、公告、研报三路信息。"""
    from app.llm.client import LLMClient
    llm = LLMClient()

    # 整理新闻文本（带 [新闻] 标签）
    if news_df.empty:
        news_text = ""
        news_count = 0
    else:
        title_col = "标题" if "标题" in news_df.columns else news_df.columns[0]
        time_col = "发布时间" if "发布时间" in news_df.columns else news_df.columns[1]
        lines = []
        for _, row in news_df.head(80).iterrows():
            t = str(row.get(time_col, ""))[:16]
            raw = str(row.get(title_col, ""))
            title = raw.split("】")[-1].strip() if "】" in raw else raw
            source = str(row.get("来源", "新闻"))
            lines.append(f"[{source}][{t}] {title[:150]}")
        news_text = "\n".join(lines)
        news_count = len(news_df)

    prompt = _build_prompt(
        session, label, now_str, news_text, news_count, today,
        notices_text=notices_text,
        research_text=research_text,
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是专业A股策略分析师。\n"
                "【铁律】只分析用户提供的信息，严禁编造、推测或引用未出现的内容。\n"
                "信息来源标注规则（必须严格执行）：\n"
                "- 每条结论末尾必须附上来源和时间，格式：[财联社 13:36] 或 [华尔街见闻 14:06] 或 [公告]\n"
                "- 时间取对应新闻行里 [...时间...] 括号内的时分部分\n"
                "- 来源标签：财联社 / 东方财富 / 华尔街见闻 / 公告 / 研报\n"
                "- 若一条结论综合多条新闻，列出最主要的一条即可\n"
                "- 严禁省略来源标注，即使结论很简短\n"
                "分析必须落地到具体A股板块和代表性个股（附股票代码），"
                "给出明确的多空方向。语言简洁，每条结论不超过3行。"
            ),
        },
        {"role": "user", "content": prompt},
    ]

    return llm.chat(messages, task_type="pro", max_tokens=3000)


def _build_prompt(
    session: SessionType,
    label: str,
    now_str: str,
    news_text: str,
    news_count: int,
    today: str,
    *,
    notices_text: str = "",
    research_text: str = "",
) -> str:
    """根据时段构建不同侧重的 Prompt，融合新闻、公告、研报三路信息。"""

    base = f"""当前时间：{now_str}，A股交易日：{today[:4]}-{today[4:6]}-{today[6:]}

【信息来源一：财联社{label}时段新闻（共{news_count}条，按时间倒序）】
{news_text if news_text else "（本时段无新闻）"}

"""
    if notices_text:
        base += f"""【信息来源二：今日上市公司重大公告（[公告]标注）】
{notices_text}

"""
    if research_text:
        base += f"""【信息来源三：近3日观察池个股券商研报（[研报]标注）】
{research_text}

"""

    if session == "pre":
        return base + """请生成【盘前快讯】，Markdown格式，必须包含以下部分：

## 🌅 盘前快讯 · {now}

### 一、隔夜关键消息（直接影响今日开盘）
（每条格式：📌 **消息** → **对A股影响** → **涉及板块/个股**）
（列3-5条最重要的，无关股市的新闻忽略）

### 二、今日板块预判
| 板块 | 方向 | 逻辑 | 代表个股 |
|---|---|---|---|
（列4-6个板块，方向用 🟢看多/🔴看空/⚪观望）

### 三、今日开盘重点关注
（列2-3个今日最值得盘前关注的具体个股/ETF，说明理由和关键价位）

### 四、今日风险提示
（1-2条，具体说明什么情况下要保守）

### 五、本日重要时间节点
（今日有无数据发布/政策/大事，影响几点钟的盘面）
""".replace("{now}", now_str)

    elif session == "mid":
        return base + """请生成【盘中半天快讯】，Markdown格式：

## ☀️ 盘中半天快讯 · {now}

### 一、上午盘面催化（新闻驱动的板块异动）
（每条格式：📌 **新闻** → **板块方向** → **个股表现预判**）
（只写与A股直接相关的，列3-5条）

### 二、上午热点板块梳理
| 板块 | 上午方向 | 催化逻辑 | 值得关注个股 |
|---|---|---|---|
（根据上午新闻判断，列3-5个板块）

### 三、午后操作策略
**🟢 可以关注：**（哪些板块/个股午后有延续性，理由）
**🔴 需要回避：**（哪些板块已经过热或消息出尽，理由）
**⚪ 等待观察：**（哪些逻辑还需要验证）

### 四、下午重点关注时间
（几点有重要数据/事件影响盘面）
""".replace("{now}", now_str)

    else:  # post
        return base + """请生成【盘后复盘快讯】，Markdown格式：

## 🌙 盘后复盘快讯 · {now}

### 一、今日A股核心逻辑（1-2句话总结今日市场主线）

### 二、今日新闻驱动的板块表现复盘
（每条格式：📌 **新闻事件** → **板块反应** → **是否延续到明日**）
（列4-6条，只写真正影响了股价的）

### 三、明日方向预判
| 板块 | 明日方向 | 逻辑 | 重点个股 |
|---|---|---|---|
（结合今日新闻和收盘信号，列3-5个板块）

### 四、明日重点关注（具体个股/ETF）
（列2-3个明日值得重点跟踪的标的，说明关键价位和买入逻辑）

### 五、明日风险提示
（1-2条，说明什么情况下明日要谨慎）
""".replace("{now}", now_str)


def _headline(content: str) -> str:
    """
    从报告正文提取标题摘要，优先取第一条 📌 催化事件。
    自动剥除 Markdown 格式符号（**、[]、→ 等）。
    """
    import re

    def _clean(text: str) -> str:
        text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)   # **bold** / *em*
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)    # [link](url)
        text = re.sub(r"\[[^\]]*\]", "", text)                   # [标签] 来源标记
        text = re.sub(r"[#>`~_]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    for line in content.split("\n"):
        line = line.strip()
        # 优先取 📌 开头的催化事件行
        if line.startswith("📌"):
            cleaned = _clean(line.lstrip("📌").strip())
            # 取 → 之前的主要事件部分
            part = cleaned.split("→")[0].strip()
            return part[:28] if part else cleaned[:28]

    # 退路：取第一行有意义的正文
    for line in content.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith(">") and len(line) > 8:
            return _clean(line)[:28]

    return "A股快讯"


def _window_desc(start_h: int, end_h: int) -> str:
    """生成时间窗口描述，如 '昨日15:00 → 今日09:00'。end_h==-1 表示动态当前时刻。"""
    if start_h < 0:
        return f"昨日{24 + start_h:02d}:00 → 今日{end_h:02d}:00"
    if end_h == -1:
        return f"今日{start_h:02d}:00 → 当前时刻"
    return f"今日{start_h:02d}:00 → {end_h:02d}:00"


def _offset_date(date_str: str, days: int) -> str:
    dt = datetime.datetime.strptime(date_str, "%Y%m%d") + datetime.timedelta(days=days)
    return dt.strftime("%Y%m%d")
