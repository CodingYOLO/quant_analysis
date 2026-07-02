"""
市场「活跃度排名」：服务器端自算的关注度代理，**替代东财人气榜的家用脚本依赖**。

为什么：东财人气榜封云服务器IP、只能住宅IP拉(家用脚本)，依赖本地不稳。
本模块用 Tushare `daily_basic`(换手率+流通市值)算全市场活跃度排名——**服务器直连、可回填历史、每日cron自记**，
喂进 hot_rank_log(kind='activity')，人气反转选股直接复用同一套轨迹/筛选逻辑(数据源无关)。

活跃度口径（可调）：换手率(关注强度·量纲无关) 与 流通成交额≈换手×流通市值(真金白银·防微盘刷屏) 两个排名取和。
  → 两者都高才是真"人气高"；纯换手会被北交所微盘刷屏，纯成交额会被巨头霸榜，混合更稳。
诚实：这是"成交活跃度"代理，非东财"自选/搜索"软人气；对"曾活跃→冷落→回暖"反转机制更客观、更难被刷。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_TOP_N = 1000   # 每日只记最活跃前 N（覆盖 峰值≤100 + 谷值≤800 窗口·省库）


def log_activity_rank(provider, trade_date: str, top: int = _TOP_N) -> int:
    """算某交易日全市场活跃度排名并落 hot_rank_log(kind='activity')。返回写入条数。"""
    from app.strategy import db
    ranked = _activity_ranked(provider, trade_date)
    if ranked is None or ranked.empty:
        return 0
    name_map = _name_map(provider)
    rows = []
    for i, ts in enumerate(ranked["ts_code"].head(top).tolist()):
        rows.append({"code": str(ts).split(".")[0], "name": name_map.get(ts, ""), "rank": i + 1})
    return db.log_hot_rank("activity", rows, trade_date)


def backfill_activity(provider, days: int = 20) -> dict:
    """回填最近 N 交易日活跃度排名——让人气反转**当天即可用**(无需攒2周·无需家用脚本)。"""
    import datetime

    from app.factors.breadth_qfq import _recent_trade_dates
    dates = _recent_trade_dates(provider, datetime.date.today().strftime("%Y%m%d"), days)
    total, ok_days = 0, 0
    for d in dates:
        try:
            n = log_activity_rank(provider, d)
            total += n
            ok_days += 1 if n else 0
        except Exception as e:
            logger.debug("[活跃度] %s 回填失败: %s", d, e)
    return {"days_requested": len(dates), "days_ok": ok_days, "rows": total,
            "range": [dates[0], dates[-1]] if dates else []}


def _activity_ranked(provider, trade_date: str):
    """取某日 daily_basic → 活跃度排名。IO 壳，排名逻辑在纯函数 _blend_rank。"""
    return _blend_rank(provider.get_daily_basic(trade_date))


def _blend_rank(dbf):
    """按「换手率排名 + 流通成交额排名」之和 升序 → 活跃度排名(第1名最活跃)。纯函数·可测。

    微盘(高换手但流通小)在成交额排名靠后 → 混合后被降权；巨头(大成交额但换手低)换手排名靠后 → 也不霸榜。
    """
    import pandas as pd
    if dbf is None or dbf.empty:
        return None
    d = dbf[["ts_code", "turnover_rate", "circ_mv"]].copy()
    d["turnover_rate"] = pd.to_numeric(d["turnover_rate"], errors="coerce")
    d["circ_mv"] = pd.to_numeric(d["circ_mv"], errors="coerce")
    d = d.dropna(subset=["turnover_rate", "circ_mv"])
    d = d[(d["circ_mv"] > 0) & (d["turnover_rate"] > 0)]
    if d.empty:
        return None
    d["amount_proxy"] = d["turnover_rate"] * d["circ_mv"]           # ≈流通成交额
    r_turn = d["turnover_rate"].rank(ascending=False, method="first")
    r_amt = d["amount_proxy"].rank(ascending=False, method="first")
    d["blend"] = r_turn + r_amt
    return d.sort_values("blend").reset_index(drop=True)


def _name_map(provider) -> dict:
    try:
        sb = provider.get_stock_basic()
        return dict(zip(sb["ts_code"], sb["name"])) if sb is not None and not sb.empty else {}
    except Exception:
        return {}
