"""
认知脚手架：把吴川式「5问框架」变成可**每日练习 + 事后校准**的结构。

设计哲学（回应用户"我要真的学会，不是跟风抄"）：
  不给答案，给「提问顺序 + 思维模型 + 数据入口」——逼你自己走一遍、留下判断，
  N 天后用**客观数据回看命中**，靠校准长出自己的交易系统。
  认知是校准出来的，不是读出来的。

本模块：① FIVE_Q 框架常量（教学本体）② daily_snapshot 轻量今日速览（复用现成看板）
③ review_calibrate 事后客观校准（上证自记录日起涨跌）。判断与记录由用户填、落 cognition_log。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 「5问框架」= 每轮行情都按这个顺序自问一遍。是教学本体，也驱动前端表单。
#   look=该看什么数据 · model=背后可迁移的思维模型 · link=去哪个现成页看数据 · field=落库字段
FIVE_Q: list[dict] = [
    {"id": "q1_regime", "no": "①", "tag": "定性", "field": "q1_regime",
     "title": "我在什么市场里？",
     "question": "普涨牛 / 结构牛 / 震荡 / 熊？指数涨 ≠ 你能赚。先给市场定性。",
     "model": "指数会骗人，结构不会。结构牛=少数主线狂涨、多数票阴跌。定性决定你该抱主线还是分散、该激进还是保守。",
     "look": "指数分化(上证 vs 科创50 vs 微盘) · 赚钱效应(涨跌家数/亏钱比例) · 风格(大小盘)",
     "link": "/overview", "link_txt": "🩺 大盘体检"},
    {"id": "q2_mainline", "no": "②", "tag": "主线", "field": "q2_mainline",
     "title": "钱在往哪集中？",
     "question": "这轮真主线是谁？只有 1-2 条。抓主线的回调，远胜在杂毛里翻找。",
     "model": "主线 = 最强产业趋势 × 最大资金共识。龙头市值扩张(范式转移)+上游涨价(真需求) 验证主线没走坏。",
     "look": "板块季/半年涨幅排序 · 龙头市值对比 · 产业链上游景气(材料/设备涨价)",
     "link": "/chain", "link_txt": "🔗 产业链地图"},
    {"id": "q3_tempo", "no": "③", "tag": "节奏", "field": "q3_tempo",
     "title": "该进还是该等？",
     "question": "现在是进场点还是等待点？同一逻辑，情绪高点追=挨打，低点埋伏=收集带血筹码。",
     "model": "节奏错，对的票也亏。情绪高不追、情绪低+主线不变时埋伏。这正是「入局区间/人气反转」在干的事。",
     "look": "情绪温度 · 连板高度 · 成交额变化 · 5日线占比(超买超卖) · 个股在人气/价格的位置",
     "link": "/sentiment", "link_txt": "🌡️ 大盘情绪"},
    {"id": "q4_catalyst", "no": "④", "tag": "催化", "field": "q4_catalyst",
     "title": "为什么是现在？",
     "question": "什么在推动下一步？没有新催化的上涨=纯情绪，易见顶。",
     "model": "趋势要有催化才持续。催化剂=验证逻辑没坏 + 给资金上涨的理由(涨价/政策/财报/机构加仓)。",
     "look": "产业链涨价/景气 · 政策定调 · 海外龙头财报 · 机构研报/仓位动向",
     "link": "/market", "link_txt": "📡 行情中枢(快讯)"},
    {"id": "q5_path", "no": "⑤", "tag": "风险·路径", "field": "q5_path",
     "title": "可能怎么演？各多大概率？",
     "question": "别赌单一方向。列几条路径 + 概率 + 触发条件 + 应对。",
     "model": "概率思维：你不需要预测对，你需要每条路都准备好怎么办。概率数字的价值在于逼你把可能性想全。",
     "look": "宏观事件日历(FOMC) · 解禁减持 · 估值拥挤度 · 中报预告雷",
     "link": "/market", "link_txt": "📅 行情中枢(日历)"},
]

STANCES = ["进攻", "均衡", "防守", "空仓"]


# ── 今日速览（轻量·复用现成看板·robust）──────────────────────────────────────
def daily_snapshot(provider) -> dict:
    """今日结构速览：情绪/量能/涨跌比/连板 + 今日风格 + 上证收盘。每源独立 try，坏一个不塌全页。"""
    import datetime

    from app.factors.breadth_qfq import _recent_trade_dates
    out = {"ok": True, "as_of": "", "kpi": {}, "style": {}, "sh_close": None}
    try:
        end = _recent_trade_dates(provider, datetime.date.today().strftime("%Y%m%d"), 1)[-1]
    except Exception:
        end = datetime.date.today().strftime("%Y%m%d")
    out["as_of"] = end
    out["kpi"] = _safe(lambda: _kpi(end)) or {}
    out["style"] = _safe(lambda: _style(provider)) or {}
    out["sh_close"] = _safe(lambda: _sh_close(provider, end))
    return out


def _kpi(end: str) -> dict:
    from app.strategy.market_sentiment import build_dashboard
    k = (build_dashboard(end) or {}).get("kpi", {}) or {}
    keep = ("temp", "amount_wy", "amount_chg_yi", "lianban_height", "limit_up",
            "limit_down", "ad_ratio", "up_count", "down_count")
    return {x: k.get(x) for x in keep if k.get(x) is not None}


def _style(provider) -> dict:
    from app.strategy.tech_chain import today_style
    s = today_style(provider) or {}
    return {"text": s.get("text"), "lean": s.get("lean")}


def _sh_close(provider, end: str):
    df = provider.get_index_daily("000001.SH", end)
    if df is not None and not df.empty:
        import pandas as pd
        v = pd.to_numeric(df["close"], errors="coerce").dropna()
        return round(float(v.iloc[-1]), 2) if len(v) else None
    return None


# ── 事后校准：过去每条推演，回看上证自记录日起的客观涨跌 ─────────────────────
def review_calibrate(provider, entries: list[dict]) -> list[dict]:
    """给历史推演补客观锚：上证自记录当日收盘至今的涨跌%（有 sh_close 才算）。

    只提供**客观事实**（大盘走向），对错由用户结合自己那天的立场自评——校准靠自己想，不代下判断。
    """
    cur = _safe(lambda: _sh_close(provider,
                                  _latest_date(provider)))
    out = []
    for e in entries:
        base = e.get("sh_close")
        sh_ret = round((cur / base - 1) * 100, 2) if (cur and base) else None
        out.append({**e, "sh_ret_since": sh_ret, "sh_now": cur})
    return out


def _latest_date(provider) -> str:
    import datetime

    from app.factors.breadth_qfq import _recent_trade_dates
    try:
        return _recent_trade_dates(provider, datetime.date.today().strftime("%Y%m%d"), 1)[-1]
    except Exception:
        return datetime.date.today().strftime("%Y%m%d")


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        logger.debug("[cognition] 源取数失败: %s", e, exc_info=True)
        return None
