"""
单股长周期 K 线加载（前复权，回测/形态识别共用的地基）。

前复权 = 原始价 × (当日复权因子 / 区间最新复权因子)，消除除权跳空，
保证个股历史序列连续——形态识别与回测的正确性命门。
"""

from __future__ import annotations

import pandas as pd

from app.data.provider import DataProvider

# 输出统一列
_COLS = ["trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]


def apply_qfq(daily: pd.DataFrame, adj: pd.DataFrame) -> pd.DataFrame:
    """
    前复权（纯函数，便于单测）：OHLC × (当日因子 / 最新因子)。
    daily/adj 均含 trade_date；adj 含 adj_factor。无因子时原样返回。
    """
    if daily is None or daily.empty:
        return pd.DataFrame(columns=_COLS)
    df = daily.sort_values("trade_date").reset_index(drop=True)
    if adj is None or adj.empty or "adj_factor" not in adj.columns:
        return df
    fmap = dict(zip(adj["trade_date"].astype(str), pd.to_numeric(adj["adj_factor"], errors="coerce")))
    f = df["trade_date"].astype(str).map(fmap)
    latest = f.dropna().iloc[-1] if f.notna().any() else None
    if not latest:
        return df
    ratio = f / latest
    out = df.copy()
    for col in ("open", "high", "low", "close"):
        if col in out.columns:
            out[col] = (pd.to_numeric(out[col], errors="coerce") * ratio).round(3)
    return out


def load_kline(ts_code: str, start: str, end: str,
               provider: DataProvider, adj: str = "qfq") -> pd.DataFrame:
    """
    返回单股 [trade_date, open, high, low, close, vol, amount, pct_chg]（按日升序）。

    Args:
        ts_code: Tushare 代码，如 '600519.SH'。
        start/end: YYYYMMDD。
        adj: 'qfq' 前复权（默认，回测/形态用）/ 'none' 不复权。
    """
    daily = provider.get_stock_daily(ts_code, start, end)
    if daily is None or daily.empty:
        return pd.DataFrame(columns=_COLS)
    if adj == "qfq":
        adj_df = provider.get_adj_factor_series(ts_code, start, end)
        daily = apply_qfq(daily, adj_df)
    else:
        daily = daily.sort_values("trade_date").reset_index(drop=True)
    keep = [c for c in _COLS if c in daily.columns]
    return daily[keep].reset_index(drop=True)
