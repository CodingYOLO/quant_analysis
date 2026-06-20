"""
大盘状态（market regime）判定：给回测的每个信号日打"强势/震荡/弱势"标签。

判定口径（以一条主指数的均线结构，便宜且可解释）：
  - 收盘同时站上 MA20 与 MA60  → 强势（趋势健康）
  - 收盘同时跌破 MA20 与 MA60  → 弱势（趋势走坏）
  - 其余（一上一下）           → 震荡

设计：分类为纯函数（可单测、零 I/O），指数取数走 CompositeProvider（依赖注入）。
"""

from __future__ import annotations

import math

import pandas as pd

# 三态标签（顺序：强→弱，前端按此展示）
REGIMES: tuple[str, str, str] = ("强势", "震荡", "弱势")

# 可选主指数（默认沪深300）。code 为 Tushare index_daily 口径。
INDEX_PRESETS: list[dict[str, str]] = [
    {"code": "000300.SH", "label": "沪深300"},
    {"code": "000001.SH", "label": "上证指数"},
    {"code": "399006.SZ", "label": "创业板指"},
    {"code": "000905.SH", "label": "中证500"},
    {"code": "000852.SH", "label": "中证1000"},
]
_INDEX_LABELS: dict[str, str] = {x["code"]: x["label"] for x in INDEX_PRESETS}
DEFAULT_INDEX = "000300.SH"


def index_label(code: str) -> str:
    """指数代码 → 可读名（未知则回显代码）。"""
    return _INDEX_LABELS.get(code, code)


# ──────────────────────────────────────────────
# 纯函数：分类 + 构建日期→状态映射
# ──────────────────────────────────────────────

def classify(close: float, ma20: float, ma60: float) -> str:
    """
    单日大盘状态分类。close/ma20 必需；ma60 早期可能缺（回退用 ma20 同侧）。
    数据不足（close 或 ma20 为 NaN）返回 ''。
    """
    if _isnan(close) or _isnan(ma20):
        return ""
    above20 = close > ma20
    above60 = (close > ma60) if not _isnan(ma60) else above20
    if above20 and above60:
        return "强势"
    if not above20 and not above60:
        return "弱势"
    return "震荡"


def build_regime_map(index_df: pd.DataFrame) -> dict[str, str]:
    """
    指数日线 → {trade_date(YYYYMMDD): 状态标签}。
    入参需含 trade_date、close；内部按日升序并算 MA20/MA60。
    """
    if index_df is None or index_df.empty or "close" not in index_df.columns:
        return {}
    df = index_df.sort_values("trade_date").reset_index(drop=True)
    close = pd.to_numeric(df["close"], errors="coerce")
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    out: dict[str, str] = {}
    for d, c, m20, m60 in zip(df["trade_date"].astype(str), close, ma20, ma60):
        out[d] = classify(c, m20, m60)
    return out


def occupancy(regime_map: dict[str, str], dates: list[str]) -> dict[str, float]:
    """给定交易日列表，统计各状态占比（%，保留1位）。无有效日返回全 0。"""
    labels = [regime_map.get(d, "") for d in dates]
    labels = [x for x in labels if x in REGIMES]
    n = len(labels)
    if n == 0:
        return {r: 0.0 for r in REGIMES}
    return {r: round(labels.count(r) / n * 100, 1) for r in REGIMES}


# ──────────────────────────────────────────────
# I/O 边界：拉指数区间并构建状态映射
# ──────────────────────────────────────────────

def load_regime_map(index_code: str, start: str, end: str, provider) -> dict[str, str]:
    """拉指数区间日线（[start, end]）并构建日期→状态映射；失败返回空映射。"""
    try:
        df = provider.get_index_daily_range(index_code, start, end)
    except Exception:
        return {}
    return build_regime_map(df)


def _isnan(v) -> bool:
    try:
        return v is None or math.isnan(float(v))
    except (TypeError, ValueError):
        return True
