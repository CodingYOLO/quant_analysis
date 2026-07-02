"""
威科夫 / 量价结构 因子（Phase 1·螺丝钉式并进因子选股）。

设计（对标用户 prompt 的辩证结论）：
  - 我们是**单日横截面筛选器**（非 IC/分层回测框架），故威科夫落成**因子列 + 阶段门控**，
    复用现有 蓄势(慢牛吸筹)/缩量/RS/资金暗流，只补真正缺的：OBV/背离/Squeeze/双顶/阶段标签。
  - **全部纯函数**：输入 t 时刻及以前的 close/high/low/vol 序列，输出标量/布尔/标签，
    **绝不看未来**（point-in-time by design·由单测证明）。
  - **A股 涨跌停量能失真**：一字板量能被压制/放大，OBV/量能类因子传入 `limit_mask`
    把涨跌停 bar 的量能贡献置零（不失真）。

诚实：量价结构是**现象描述**，不预测涨跌、不构成买卖建议（[[no-directional-recommendations]]）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _clean(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").dropna()


def obv_series(close: pd.Series, vol: pd.Series, limit_mask: pd.Series | None = None) -> pd.Series:
    """OBV = Σ sign(Δclose)·vol。涨跌停 bar(limit_mask=True) 量能贡献置零（防一字板失真）。"""
    close, vol = close.astype(float), vol.astype(float)
    direction = np.sign(close.diff().fillna(0.0))
    v = vol.copy()
    if limit_mask is not None:
        v = v.where(~limit_mask.reindex(v.index).fillna(False), 0.0)
    return (direction * v).cumsum()


def obv_slope_norm(close: pd.Series, vol: pd.Series, n: int = 20,
                   limit_mask: pd.Series | None = None) -> float | None:
    """OBV 近 n 日回归斜率 / 近 n 日均量 → 归一化净吸筹强度。>0=资金在进(吸筹签名)。"""
    close, vol = _clean(close), _clean(vol)
    if len(close) < n or len(vol) < n:
        return None
    obv = obv_series(close, vol, limit_mask).tail(n).to_numpy()
    slope = float(np.polyfit(np.arange(n), obv, 1)[0])
    avg_vol = float(vol.tail(n).mean())
    return round(slope / avg_vol, 4) if avg_vol > 0 else None


def obv_divergence(close: pd.Series, vol: pd.Series, n: int = 20,
                   limit_mask: pd.Series | None = None) -> float | None:
    """OBV 相对价格的强弱（分位差·%）：现 OBV 在近 n 日的分位 − 现价在近 n 日的分位。
    >0 = OBV 强于价（价平/价跌但 OBV 上行·吸筹/底背离）；<0 = 价强于 OBV（价涨 OBV 不跟·顶背离风险）。"""
    close, vol = _clean(close), _clean(vol)
    if len(close) < n:
        return None
    obv = obv_series(close, vol, limit_mask).tail(n)
    c = close.tail(n)
    price_rank = float((c <= c.iloc[-1]).mean())
    obv_rank = float((obv <= obv.iloc[-1]).mean())
    return round((obv_rank - price_rank) * 100, 1)


def squeeze_pctile(high: pd.Series, low: pd.Series, close: pd.Series,
                   atr_n: int = 20, lookback: int = 250) -> float | None:
    """蓄势收窄：ATR(atr_n)/close 的最新值在过去 lookback 日的分位（0~1）。越低=波动越收敛(横有多长)。"""
    high, low, close = _clean(high), _clean(low), _clean(close)
    if len(close) < atr_n + 5:
        return None
    prev = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    ratio = (tr.rolling(atr_n).mean() / close).dropna()
    if ratio.empty:
        return None
    window = ratio.tail(lookback)
    return round(float((window <= ratio.iloc[-1]).mean()), 3)


def detect_double_top(close: pd.Series, high: pd.Series, vol: pd.Series, lookback: int = 90,
                      tol: float = 0.04, break_buf: float = 0.02, min_gap: int = 15) -> bool:
    """双顶破位（只作风险过滤·非 alpha）：近 lookback 日两个等高峰(间隔>min_gap)+第二顶量能背离+收盘有效跌破颈线。"""
    close, high, vol = _clean(close), _clean(high), _clean(vol)
    if len(close) < min_gap + 10:
        return False
    h = high.tail(lookback).to_numpy()
    c = close.tail(lookback)
    if len(h) < min_gap + 10:
        return False
    peaks = [i for i in range(2, len(h) - 2)
             if h[i] >= h[i - 1] and h[i] >= h[i - 2] and h[i] >= h[i + 1] and h[i] >= h[i + 2]]
    if len(peaks) < 2:
        return False
    p1, p2 = peaks[-2], peaks[-1]
    if p2 - p1 < min_gap:
        return False
    if abs(h[p2] - h[p1]) / h[p1] > tol:                       # 两顶不等高
        return False
    v = vol.tail(lookback).to_numpy()
    if v[max(0, p2 - 2):p2 + 1].mean() >= v[max(0, p1 - 2):p1 + 1].mean():  # 第二顶未量能背离
        return False
    neckline = float(c.iloc[p1:p2 + 1].min())
    return float(c.iloc[-1]) < neckline * (1 - break_buf)      # 收盘有效跌破颈线


# ── 箱体/达瓦斯 补充因子（与威科夫互补的小增量·point-in-time）──────────────────
def near_high(close: pd.Series, high: pd.Series, n: int = 60) -> float | None:
    """NearHigh：现价 / 近 n 日最高（临近箱顶/新高动量）。→1 = 贴箱顶。"""
    close, high = _clean(close), _clean(high)
    if len(high) < 5:
        return None
    hh = float(high.tail(n).max())
    return round(float(close.iloc[-1]) / hh, 3) if hh > 0 else None


def box_age(high: pd.Series, low: pd.Series, lookback: int = 60) -> int:
    """横盘天数 = 距最近一次创 lookback 日新高/新低的连续天数（"横有多长"·因果定律的因）。"""
    high, low = _clean(high), _clean(low)
    if len(high) < 6:
        return 0
    win = min(lookback, len(high))
    hs, ls = high.tail(win).to_numpy(), low.tail(win).to_numpy()
    age = 0
    for i in range(len(hs) - 1, 0, -1):                        # 从今日往回·只用 ≤i 的数据
        if hs[i] >= hs[:i + 1].max() or ls[i] <= ls[:i + 1].min():
            break                                             # 该日创了新高或新低 → 箱体在此重置
        age += 1
    return age


def false_breakout(close: pd.Series, high: pd.Series, lookback: int = 60,
                   k: int = 3, buf: float = 0.01) -> bool:
    """假突破(威科夫UT陷阱)：近 k 日曾收盘突破前箱顶，但现价又跌回箱内 → 风险过滤(非买入)。"""
    close, high = _clean(close), _clean(high)
    if len(close) < lookback + k:
        return False
    prior_top = float(high.iloc[-(lookback + k):-k].max())    # k 日前的箱顶(不含突破那几日)
    broke = bool((close.tail(k) > prior_top * (1 + buf)).any())
    back_in = float(close.iloc[-1]) <= prior_top
    return broke and back_in


def detect_double_bottom(close: pd.Series, low: pd.Series, vol: pd.Series, lookback: int = 90,
                         tol: float = 0.04, break_buf: float = 0.02, min_gap: int = 15) -> bool:
    """双底突破（并入突破族·非独立alpha）：近 lookback 日两个等低谷(间隔>min_gap)+第二谷缩量二次探底+收盘突破颈线。"""
    close, low, vol = _clean(close), _clean(low), _clean(vol)
    if len(close) < min_gap + 10:
        return False
    lo = low.tail(lookback).to_numpy()
    c = close.tail(lookback)
    if len(lo) < min_gap + 10:
        return False
    troughs = [i for i in range(2, len(lo) - 2)
               if lo[i] <= lo[i - 1] and lo[i] <= lo[i - 2] and lo[i] <= lo[i + 1] and lo[i] <= lo[i + 2]]
    if len(troughs) < 2:
        return False
    p1, p2 = troughs[-2], troughs[-1]
    if p2 - p1 < min_gap:
        return False
    if abs(lo[p2] - lo[p1]) / lo[p1] > tol:                   # 两谷不等低
        return False
    v = vol.tail(lookback).to_numpy()
    if v[max(0, p2 - 2):p2 + 1].mean() >= v[max(0, p1 - 2):p1 + 1].mean():  # 第二谷未缩量
        return False
    neckline = float(c.iloc[p1:p2 + 1].max())                 # 两谷间最高收盘=颈线
    return float(c.iloc[-1]) > neckline * (1 + break_buf)     # 收盘突破颈线


def wyckoff_phase(close: pd.Series, high: pd.Series, low: pd.Series, vol: pd.Series,
                  limit_mask: pd.Series | None = None) -> str:
    """威科夫阶段标签（门控用·现象描述非买卖建议）：派发破位 / SOS突破 / Spring / 吸筹候选 / —。"""
    close, high, low, vol = _clean(close), _clean(high), _clean(low), _clean(vol)
    if len(close) < 60:
        return "—"
    if detect_double_top(close, high, vol, lookback=90):
        return "派发破位"
    cur = float(close.iloc[-1])
    hh60 = float(high.tail(60).iloc[:-1].max())               # 不含今日的60日高
    v = vol.tail(20)
    vma20 = float(v.mean()) if len(v) else 0.0
    if cur > hh60 and vma20 > 0 and float(vol.iloc[-1]) > 2 * vma20:
        return "SOS突破"                                       # 放量突破60日高
    ll = float(low.tail(20).iloc[:-1].min())                   # 不含今日的20日低
    if float(low.iloc[-1]) < ll and cur > ll and float(vol.iloc[-1]) < vma20:
        return "Spring"                                        # 假破位缩量承接
    obv_sl = obv_slope_norm(close, vol, 20, limit_mask)
    sq = squeeze_pctile(high, low, close, 20, 250)
    if obv_sl is not None and obv_sl > 0 and sq is not None and sq <= 0.3:
        return "吸筹候选"                                       # 蓄势收窄 + OBV上行
    return "—"
