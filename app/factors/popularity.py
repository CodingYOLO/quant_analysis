"""
M3：人气体系（换手率全市场排名代理）。

设计取舍：东财股吧人气榜端点不稳定（时有 RemoteDisconnected），改用
Tushare daily_basic 的「换手率全市场排名」作为人气代理：
  - 优点：100% 可达、及时、可回补历史；盘后随宽表一并计算，无需单独抓取 cron。
  - 局限：是「活跃度」代理，非东财股吧「人气」原口径；且为盘后单快照，
    无早盘/收盘之分 → 收盘-早盘恒为 0（如实标注，不伪造日内变化）。

口径：换手率(turnover_rate)全市场降序 → 名次(1=最活跃) → 单调递减变换得权重。
派生主题级 pop_weight/HHI/fairness（成分股权重聚合）。

【需校准 C3】rank→权重变换 w=clip(a·exp(-rank/k),0,1)。
锚点（东财人气榜实测 rank27→0.19、rank92→0.10）暂沿用；代理口径下需另行回归。
"""

from __future__ import annotations

import logging
import math

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.theme_heat_db import upsert_popularity_full

logger = logging.getLogger(__name__)

# rank→权重拟合参数（沿用东财锚点拟合；代理口径【需校准】）
_POP_W_A = 0.248
_POP_W_K = 101.3
# 仅保存名次靠前的（权重>0 的有意义部分），控制表体量
_STORE_TOP_N = 1000


def rank_to_weight(rank: int | None) -> float | None:
    """活跃度名次 → 权重（名次越前越高）。rank 缺失返回 None。"""
    if rank is None or rank <= 0:
        return None
    return round(min(max(_POP_W_A * math.exp(-rank / _POP_W_K), 0.0), 1.0), 4)


def build_popularity_proxy(trade_date: str, provider: CompositeProvider | None = None) -> dict[str, float]:
    """
    用换手率全市场排名计算人气代理权重，落库 popularity_rank，并返回 {ts_code: 权重}。

    Args:
        trade_date: 交易日 YYYYMMDD
        provider:   数据接口

    Returns:
        {ts_code: 日内综合权重}；当日 daily_basic 缺失返回 {}（数据缺失，不伪造）。
    """
    provider = provider or CompositeProvider()
    try:
        db = provider.get_daily_basic(trade_date)
    except Exception as e:
        logger.warning("[人气代理] %s daily_basic 失败: %s", trade_date, e)
        return {}
    if db is None or db.empty or "turnover_rate" not in db.columns:
        logger.warning("[人气代理] %s 无换手率数据", trade_date)
        return {}

    df = db[["ts_code", "turnover_rate"]].copy()
    df["_tr"] = pd.to_numeric(df["turnover_rate"], errors="coerce")
    df = df.dropna(subset=["_tr"]).sort_values("_tr", ascending=False).reset_index(drop=True)

    weights: dict[str, float] = {}
    rows: list[tuple] = []
    for i, ts in enumerate(df["ts_code"]):
        rank = i + 1
        w = rank_to_weight(rank)
        weights[ts] = w
        if rank <= _STORE_TOP_N:
            rows.append((ts, rank, w))

    upsert_popularity_full(trade_date, rows)
    logger.info("[人气代理] %s 换手率排名 %d 只，存 Top%d", trade_date, len(df), len(rows))
    return weights


# ──────────────────────────────────────────────
# 主题级聚合（供 theme_wide 调用）
# ──────────────────────────────────────────────

def theme_pop_factors(codes: list[str], weight_map: dict[str, float]) -> dict[str, float | None]:
    """
    主题级人气因子：pop_weight(均值) / pop_concentration_hhi / pop_fairness(1-Gini)。
    成分股均无权重时返回 None（数据缺失，不置零）。
    """
    ws = [weight_map[c] for c in codes if c in weight_map and weight_map[c] is not None]
    if not ws:
        return {"pop_weight": None, "pop_concentration_hhi": None, "pop_fairness": None}
    total = sum(ws)
    pop_weight = round(sum(ws) / len(ws), 4)
    hhi = round(sum((w / total) ** 2 for w in ws) * 100, 2) if total > 0 else None
    fairness = round(1 - _gini(ws), 4)
    return {"pop_weight": pop_weight, "pop_concentration_hhi": hhi, "pop_fairness": fairness}


def _gini(values: list[float]) -> float:
    """基尼系数（0=完全均衡，1=完全集中）。"""
    if not values:
        return 0.0
    xs = sorted(values)
    n = len(xs)
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    s = sum(xs)
    if s == 0:
        return 0.0
    return (2 * cum) / (n * s) - (n + 1) / n
