"""
M1：前复权多档均线广度（板块内部健康度核心因子）。

广度 breadth_ma_x = 板块内「前复权收盘价 ≥ x 日前复权均线」的股票占比（%）。
用前复权（qfq）避免除权日污染均线。解读范式：
  短期广度高(MA5) + 长期广度低(MA90/144) = 短期普涨但中期未站稳 = 主升初期需回踩。

实现要点（性能）：
  - 复权一致性：判断 close≥MA 只需 same-stock 价格序列复权口径一致；
    `close × adj_factor`（后复权绝对值）与前复权对「≥」判断完全等价（线性缩放不改变大小关系），
    因此 panel 直接存 `close×adj_factor`，无需再除基准。
  - panel 全市场只构建一次（按 end_date 缓存），各主题切片复用，不重复拉数。

数据：CompositeProvider.get_daily + get_adj_factor（均按交易日缓存 parquet）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

# 标准 8 档均线窗口（与吴川参考站一致）
BREADTH_WINDOWS = (3, 5, 10, 20, 30, 60, 90, 144)


def _recent_trade_dates(provider: CompositeProvider, end_date: str, n: int) -> list[str]:
    """返回截至 end_date（含）的最近 n 个交易日（升序）。"""
    start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=int(n * 1.7) + 20)).strftime("%Y%m%d")
    cal = provider.get_trade_cal(start, end_date)
    dates = sorted(cal[cal["is_open"] == 1]["cal_date"].tolist())
    return dates[-n:]


def build_qfq_panel(
    end_date: str,
    provider: CompositeProvider | None = None,
    lookback: int = 145,
) -> pd.DataFrame:
    """
    构建全市场前复权收盘价面板。

    Args:
        end_date: 截止交易日 YYYYMMDD
        provider: 数据接口
        lookback: 回溯交易日数（默认 145，覆盖 MA144）

    Returns:
        DataFrame：index=ts_code，columns=交易日(升序)，值=close×adj_factor。
        数据不足返回空表。
    """
    provider = provider or CompositeProvider()
    dates = _recent_trade_dates(provider, end_date, lookback)
    if not dates:
        return pd.DataFrame()

    cols: dict[str, pd.Series] = {}
    for d in dates:
        try:
            daily = provider.get_daily(d)
            adj = provider.get_adj_factor(d)
        except Exception as e:
            logger.debug("[广度] %s 取数失败: %s", d, e)
            continue
        if daily is None or daily.empty or adj is None or adj.empty:
            continue
        m = daily[["ts_code", "close"]].merge(adj[["ts_code", "adj_factor"]], on="ts_code", how="inner")
        qfq = pd.to_numeric(m["close"], errors="coerce") * pd.to_numeric(m["adj_factor"], errors="coerce")
        cols[d] = qfq.set_axis(m["ts_code"])

    if not cols:
        return pd.DataFrame()
    panel = pd.DataFrame(cols)
    return panel.reindex(sorted(panel.columns), axis=1)


def compute_breadth(
    panel: pd.DataFrame,
    ts_codes: list[str],
    windows: tuple[int, ...] = BREADTH_WINDOWS,
) -> dict[str, float | None]:
    """
    在给定面板上，计算某主题成分股的多档均线广度。

    Args:
        panel:    build_qfq_panel 的输出（全市场）
        ts_codes: 主题成分股
        windows:  均线窗口档位

    Returns:
        {'ma3': 88.0, 'ma5': ..., ...}；某档数据不足为 None。
    """
    if panel is None or panel.empty or not ts_codes:
        return {f"ma{w}": None for w in windows}

    sub = panel.reindex(ts_codes)
    cur = sub[panel.columns[-1]]                 # 截止日前复权收盘
    out: dict[str, float | None] = {}
    for w in windows:
        if panel.shape[1] < w:
            out[f"ma{w}"] = None
            continue
        ma = sub[panel.columns[-w:]].mean(axis=1)
        valid = cur.notna() & ma.notna() & (ma > 0)
        out[f"ma{w}"] = round(float((cur[valid] >= ma[valid]).mean()) * 100, 1) if valid.sum() else None
    return out
