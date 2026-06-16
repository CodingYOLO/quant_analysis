"""
M3：人气体系（东财人气榜前向积累）。

数据源：akshare stock_hot_rank_em（东财人气榜，Top100 当前快照，服务器可达）。
调度：每交易日抓 2 次——早盘约 09:45（am）、收盘约 15:05（pm）→ popularity_rank 表。
派生：
  - 个股「日内综合权重」= 人气排名经单调递减变换；排名越前权重越高。
  - 收盘-早盘 = pm_rank - am_rank（负=日内人气上行=更热）。
  - 主题级人气因子（pop_weight/HHI/fairness）= 成分股日内综合权重的聚合（见 theme_wide）。

【需校准 C3】rank→权重变换：w = clip(a·exp(-rank/k), 0, 1)。
已知锚点（吴川截图实测）：rank27→0.19、rank92→0.10，据此拟合 a、k（抽常量便于回归）。
仅 Top100 深度；无历史回补——启用前的交易日人气字段为 None（数据缺失）。
"""

from __future__ import annotations

import logging
import math

from app.data.composite_provider import CompositeProvider
from app.data.theme_heat_db import (
    get_popularity,
    update_popularity_weights,
    upsert_popularity,
)

logger = logging.getLogger(__name__)

# rank→权重拟合参数（由锚点 rank27→0.19、rank92→0.10 反解；【需校准】可调）
_POP_W_A = 0.248
_POP_W_K = 101.3


def rank_to_weight(rank: int | None) -> float | None:
    """人气排名 → 权重（排名越前越高）。rank 缺失返回 None。"""
    if rank is None or rank <= 0:
        return None
    return round(min(max(_POP_W_A * math.exp(-rank / _POP_W_K), 0.0), 1.0), 4)


# ──────────────────────────────────────────────
# 抓取
# ──────────────────────────────────────────────

def capture_hot_rank(trade_date: str, slot: str, provider: CompositeProvider | None = None) -> int:
    """
    抓取东财人气榜当前快照，写入 popularity_rank 的对应时段列。

    Args:
        trade_date: 交易日 YYYYMMDD
        slot:       'am' 早盘 / 'pm' 收盘
        provider:   数据接口（用于 symbol→ts_code 映射）

    Returns:
        写入条数；失败/无数据返回 0。
    """
    provider = provider or CompositeProvider()
    try:
        df = provider._ak.get_hot_rank()
    except Exception as e:
        logger.warning("[人气] 东财人气榜抓取失败（已重试）: %s", e)
        return 0
    if df is None or df.empty:
        logger.warning("[人气] 东财人气榜返回空")
        return 0

    rank_col = next((c for c in df.columns if "排名" in c), None)
    code_col = next((c for c in df.columns if "代码" in c), None)
    if not rank_col or not code_col:
        logger.warning("[人气] 人气榜字段异常: %s", list(df.columns))
        return 0

    sym2ts = _symbol_to_tscode(provider)
    rank_map: dict[str, int] = {}
    for _, r in df.iterrows():
        sym = str(r[code_col]).strip()[-6:]   # 兼容带/不带市场前缀
        ts = sym2ts.get(sym)
        try:
            rank = int(r[rank_col])
        except (TypeError, ValueError):
            continue
        if ts:
            rank_map[ts] = rank

    n = upsert_popularity(trade_date, slot, rank_map)
    logger.info("[人气] %s %s 写入 %d 条", trade_date, slot, n)
    return n


def _symbol_to_tscode(provider: CompositeProvider) -> dict[str, str]:
    """6 位代码 → Tushare ts_code。"""
    sb = provider.get_stock_basic()
    return dict(zip(sb["symbol"].astype(str), sb["ts_code"].astype(str)))


# ──────────────────────────────────────────────
# 权重派生
# ──────────────────────────────────────────────

def compute_weights(trade_date: str) -> int:
    """
    依据当日 am_rank/pm_rank 计算权重列。

    intraday_weight 取 早/收 权重均值（缺一则用另一个）；
    equiv_rank 取 早/收 排名均值（跨日可比的归一化人气排名近似）。
    """
    recs = get_popularity(trade_date)
    if not recs:
        return 0
    updates = []
    for r in recs:
        am, pm = r.get("am_rank"), r.get("pm_rank")
        aw, pw = rank_to_weight(am), rank_to_weight(pm)
        ws = [w for w in (aw, pw) if w is not None]
        iw = round(sum(ws) / len(ws), 4) if ws else None
        ranks = [x for x in (am, pm) if x is not None]
        eq = int(round(sum(ranks) / len(ranks))) if ranks else None
        updates.append((aw, pw, iw, eq, r["ts_code"]))
    n = update_popularity_weights(trade_date, updates)
    logger.info("[人气] %s 权重回填 %d 条", trade_date, n)
    return n


# ──────────────────────────────────────────────
# 主题级聚合（供 theme_wide 调用）
# ──────────────────────────────────────────────

def get_intraday_weights(trade_date: str) -> dict[str, float]:
    """{ts_code: 日内综合权重}（仅当日有人气数据时非空）。"""
    out: dict[str, float] = {}
    for r in get_popularity(trade_date):
        w = r.get("intraday_weight")
        if w is not None:
            out[r["ts_code"]] = float(w)
    return out


def theme_pop_factors(codes: list[str], weight_map: dict[str, float]) -> dict[str, float | None]:
    """
    主题级人气因子：pop_weight(均值) / pop_concentration_hhi / pop_fairness(1-Gini)。
    成分股均无人气数据时返回 None（数据缺失，不置零）。
    """
    ws = [weight_map[c] for c in codes if c in weight_map]
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
