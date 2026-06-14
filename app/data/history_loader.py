"""
历史行情批量加载器。
通过按日期拉取全市场日线数据（已缓存），构建个股价格/成交量矩阵。
避免逐只股票拉取，提高效率。
"""

import logging
from datetime import datetime, timedelta

import pandas as pd

from app.data.provider import DataProvider

logger = logging.getLogger(__name__)


def _get_prior_dates(trade_date: str, provider: DataProvider, n: int) -> list[str]:
    """
    获取 trade_date 之前（含当日）的 n 个交易日列表，从交易日历获取。
    """
    dt = datetime.strptime(trade_date, "%Y%m%d")
    start_dt = dt - timedelta(days=n * 2)  # 多取一倍，过滤非交易日
    start_str = start_dt.strftime("%Y%m%d")

    cal = provider.get_trade_cal(start_str, trade_date)
    open_days = (
        cal[cal["is_open"] == 1]["cal_date"]
        .sort_values()
        .tolist()
    )
    # 取最近 n 个交易日（含 trade_date）
    valid = [d for d in open_days if d <= trade_date]
    return valid[-n:]


def load_price_matrix(
    trade_date: str,
    provider: DataProvider,
    n_days: int = 65,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    加载最近 n_days 个交易日的全市场价格/量数据，构建矩阵。

    Returns:
        (close_matrix, open_matrix, high_matrix, low_matrix, vol_matrix)
        每个 DataFrame: index=日期(str), columns=ts_code, 值=对应价格/量
    """
    dates = _get_prior_dates(trade_date, provider, n_days)
    logger.info("加载 %d 个交易日行情数据（%s ~ %s）", len(dates), dates[0], dates[-1])

    frames = []
    for d in dates:
        df = provider.get_daily(d)
        if df is not None and not df.empty:
            df["_date"] = d
            frames.append(df)

    if not frames:
        raise ValueError(f"无法加载 {trade_date} 前 {n_days} 日的行情数据")

    all_data = pd.concat(frames, ignore_index=True)

    def _pivot(col: str) -> pd.DataFrame:
        return all_data.pivot_table(index="_date", columns="ts_code", values=col, aggfunc="last")

    close_m = _pivot("close")
    open_m = _pivot("open")
    high_m = _pivot("high")
    low_m = _pivot("low")
    vol_m = _pivot("vol")

    return close_m, open_m, high_m, low_m, vol_m


def get_stock_history(
    ts_code: str,
    close_m: pd.DataFrame,
    open_m: pd.DataFrame,
    high_m: pd.DataFrame,
    low_m: pd.DataFrame,
    vol_m: pd.DataFrame,
) -> dict[str, pd.Series]:
    """从矩阵中提取单只股票的历史序列（已按日期排序）。"""
    def _get(matrix: pd.DataFrame) -> pd.Series:
        if ts_code not in matrix.columns:
            return pd.Series(dtype=float)
        return matrix[ts_code].dropna().sort_index()

    return {
        "close": _get(close_m),
        "open": _get(open_m),
        "high": _get(high_m),
        "low": _get(low_m),
        "vol": _get(vol_m),
    }
