"""
技术因子计算库。
所有函数接收 pd.Series（价格/成交量时序），返回标量或 Series。
按列计算，不依赖任何外部服务，便于单元测试。
"""

import numpy as np
import pandas as pd


# ============================================================
# 均线
# ============================================================

def ma(series: pd.Series, n: int) -> pd.Series:
    """简单移动平均线。"""
    return series.rolling(n, min_periods=n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    """指数移动平均线。"""
    return series.ewm(span=n, adjust=False, min_periods=n).mean()


def above_ma(close: pd.Series, n: int) -> bool:
    """最新收盘价是否站上 N 日均线。"""
    ma_val = ma(close, n)
    if ma_val.iloc[-1] is np.nan or pd.isna(ma_val.iloc[-1]):
        return False
    return bool(close.iloc[-1] > ma_val.iloc[-1])


def ma_slope(close: pd.Series, n: int, lookback: int = 3) -> float:
    """N 日均线近 lookback 日斜率（正值=上升）。"""
    m = ma(close, n)
    if m.iloc[-lookback:].isna().any():
        return 0.0
    return float(m.iloc[-1] - m.iloc[-lookback]) / float(m.iloc[-lookback] + 1e-8)


# ============================================================
# MACD
# ============================================================

def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """
    返回 DataFrame，含列：dif, dea, hist。
    dif = EMA(fast) - EMA(slow)
    dea = EMA(dif, signal)
    hist = (dif - dea) * 2
    """
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = (dif - dea) * 2
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist})


def macd_golden_cross(close: pd.Series) -> bool:
    """
    判断 MACD 金叉：前一日 dif < dea，当日 dif > dea。
    同时要求 dif 在 0 轴附近（-0.05 ~ 0.1 相对于收盘价的比例），避免追顶。
    """
    if len(close) < 35:
        return False
    m = macd(close)
    if m["dif"].iloc[-2:].isna().any():
        return False
    prev_cross = m["dif"].iloc[-2] < m["dea"].iloc[-2]
    curr_cross = m["dif"].iloc[-1] >= m["dea"].iloc[-1]
    # dif 相对收盘价的比例不能过大（防止顶部死叉后的假金叉）
    dif_ratio = abs(m["dif"].iloc[-1]) / (close.iloc[-1] + 1e-8)
    return bool(prev_cross and curr_cross and dif_ratio < 0.05)


# ============================================================
# RSI
# ============================================================

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """相对强弱指标。"""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).rolling(n, min_periods=n).mean()
    rs = gain / (loss + 1e-8)
    return 100 - 100 / (1 + rs)


# ============================================================
# 量能
# ============================================================

def volume_ratio(vol: pd.Series, n: int = 5) -> float:
    """
    量比 = 今日成交量 / 近 N 日均量。
    >1.5 放量，<0.7 缩量。
    """
    if len(vol) < n + 1:
        return 1.0
    avg = vol.iloc[-(n + 1):-1].mean()
    if avg <= 0:
        return 1.0
    return float(vol.iloc[-1] / avg)


def is_shrink_volume(vol: pd.Series, threshold: float = 0.7) -> bool:
    """缩量判断：今日成交量低于近5日均量的 threshold 倍。"""
    return volume_ratio(vol) < threshold


def has_lower_shadow(open_: float, low: float, close: float, min_ratio: float = 0.5) -> bool:
    """
    下影线判断：下影线长度占实体的比例 > min_ratio。
    下影线 = min(open, close) - low
    实体 = |close - open|
    """
    body = abs(close - open_)
    shadow = min(open_, close) - low
    if body < 1e-6:
        return shadow > 0
    return shadow / body > min_ratio


# ============================================================
# RPS 相对强弱（个股相对市场的强度排名）
# ============================================================

def calc_rps(returns_matrix: pd.DataFrame, n: int) -> pd.Series:
    """
    计算全市场个股的 N 日 RPS（相对强弱百分位排名）。

    Args:
        returns_matrix: DataFrame，index=日期，columns=ts_code，值=收盘价
        n: 回溯天数（如50、120）

    Returns:
        Series，index=ts_code，值=0~100 的 RPS 分数
    """
    if len(returns_matrix) < n + 1:
        return pd.Series(dtype=float)
    start_price = returns_matrix.iloc[-(n + 1)]
    end_price = returns_matrix.iloc[-1]
    ret = (end_price - start_price) / (start_price.replace(0, np.nan))
    # 百分位排名（越高说明个股越强）
    rank = ret.rank(pct=True) * 100
    return rank


# ============================================================
# 回踩质量评分（吴川体系核心：缩量回踩均线是低吸信号）
# ============================================================

def pullback_quality_score(
    close: pd.Series,
    vol: pd.Series,
    open_: pd.Series,
    low: pd.Series,
) -> float:
    """
    回踩质量综合评分（0~100），分数越高越适合低吸。
    评分维度：
    - 站上MA20且回踩幅度<3%（40分）
    - 缩量（量比<0.7）（25分）
    - 下影线明显（15分）
    - MA20斜率向上（20分）
    """
    score = 0.0
    if len(close) < 25:
        return score

    ma20 = ma(close, 20)
    if pd.isna(ma20.iloc[-1]):
        return score

    last_close = close.iloc[-1]
    last_ma20 = ma20.iloc[-1]

    # 站上MA20且回踩幅度<3%
    deviation = (last_close - last_ma20) / (last_ma20 + 1e-8)
    if 0 <= deviation < 0.03:
        score += 40
    elif 0.03 <= deviation < 0.06:
        score += 20  # 偏离稍大，部分给分

    # 缩量
    vr = volume_ratio(vol)
    if vr < 0.7:
        score += 25
    elif vr < 0.9:
        score += 12

    # 下影线
    if has_lower_shadow(open_.iloc[-1], low.iloc[-1], last_close):
        score += 15

    # MA20斜率向上
    slope = ma_slope(close, 20, lookback=3)
    if slope > 0.001:
        score += 20
    elif slope > 0:
        score += 10

    return min(score, 100.0)


# ============================================================
# VWAP（成交量加权平均价）
# ============================================================

def calc_vwap(close: pd.Series, vol: pd.Series, n: int = 20) -> float:
    """
    计算 N 日 VWAP（主力平均持仓成本）。
    VWAP = Σ(close × vol) / Σ(vol)
    对应吴川体系的"保守买入价"参考位。
    """
    if len(close) < n or len(vol) < n:
        return float(close.iloc[-1]) if len(close) > 0 else 0.0
    c = close.iloc[-n:]
    v = vol.iloc[-n:]
    total_vol = v.sum()
    if total_vol <= 0:
        return float(close.iloc[-1])
    return float((c * v).sum() / total_vol)


def vwap_position(close: pd.Series, vol: pd.Series, n: int = 20) -> float:
    """
    个股当前价相对 VWAP 的偏离比例。
    正值 = 价格在主力成本上方（安全）
    负值 = 价格在主力成本下方（谨慎）
    """
    vwap = calc_vwap(close, vol, n)
    if vwap <= 0:
        return 0.0
    return (float(close.iloc[-1]) - vwap) / vwap


# ============================================================
# 止损止盈价格计算（吴川体系硬规则）
# ============================================================

def calc_stop_loss_price(close: pd.Series) -> float:
    """
    止损价 = MA5（跌破5日均线无条件先止损）。
    """
    if len(close) < 5:
        return float(close.iloc[-1]) * 0.95
    ma5 = ma(close, 5)
    val = ma5.iloc[-1]
    return float(val) if not pd.isna(val) else float(close.iloc[-1]) * 0.95


def calc_take_profit_prices(close: pd.Series) -> tuple[float, float]:
    """
    止盈价格（吴川体系）：
    - 止盈1：+5%，减仓一半
    - 止盈2：+8%，继续减仓
    返回 (take_profit_1, take_profit_2)
    """
    last = float(close.iloc[-1])
    return round(last * 1.05, 2), round(last * 1.08, 2)


def calc_buy_zones(
    close: pd.Series,
    vol: pd.Series,
) -> tuple[float, float]:
    """
    买入参考区间（吴川体系）：
    - 保守买入价 = 20日VWAP（主力平均成本，回踩此处低吸）
    - 激进买入价 = 当日收盘价（趋势追随）
    返回 (conservative, aggressive)
    """
    conservative = round(calc_vwap(close, vol, n=20), 2)
    aggressive = round(float(close.iloc[-1]), 2)
    return conservative, aggressive


def calc_position_pct(market_label: str) -> float:
    """
    根据市场状态返回单票建议仓位（吴川体系）：
    升温/主升：5%
    震荡/退潮反抽：3%
    弱势/衰退：0%
    """
    if market_label in ("升温", "主升"):
        return 0.05
    elif market_label in ("震荡", "退潮反抽"):
        return 0.03
    else:
        return 0.0
