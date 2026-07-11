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


# ── VPA 拉升前临界：震仓 + 测试通过（吸筹尾声·拉升前最后一道工序）──────────────
# 阈值集中于此·供回测校准（先给经验值·backtest 后按数据定）。
_VPA = {
    "min_bars": 120,          # 至少 N 根日K
    "pos_ceil": 0.55,         # 底部区域：现价在近250日区间分位 ≤ 此值（排派发箱体）
    "stop_tol": 0.99,         # 停止新低：近20日低 ≥ 近60日低 × 此值
    "test_look": 5,           # 测试窗：近 N 日内找一次缩量下探企稳
    "test_vol_n": 60,         # 量分位回看
    "test_dry_q": 0.10,       # 测试通过的量分位上限（缩到极致=无供应·要够稀有）
    "critical_score": 65,     # 临界还需整体评分≥此（少而精·避免只靠一根缩量误报）
    "test_box_n": 40,         # 箱底回看（判"守住"）
    "critical_days": 3,       # 测试发生在近 N 日内 → 临界
    "down_shrink_min": 1.10,  # 下跌缩量：涨日均量/跌日均量 ≥ 此值（卖压衰竭·吸筹签名）
    "sq_good": 0.30,          # squeeze 分位 ≤ 此值算蓄势收敛
}


def down_day_shrink(close: pd.Series, vol: pd.Series, n: int = 40) -> float | None:
    """下跌缩量度（吸筹签名·排除下跌中继）：近 n 日 涨日均量 / 跌日均量。
    >1 = 跌日比涨日缩量（卖压衰竭·吸筹）；<1 = 跌日放量（供应未尽/下跌中继）。"""
    close, vol = _clean(close), _clean(vol)
    if len(close) < n + 2:
        return None
    c = close.tail(n + 1)
    v = vol.tail(n + 1)
    chg = c.diff().dropna()
    vv = v.iloc[1:]
    up_v = vv[chg.to_numpy() > 0].mean()
    dn_v = vv[chg.to_numpy() < 0].mean()
    if not (dn_v and dn_v > 0) or not (up_v == up_v):
        return None
    return round(float(up_v) / float(dn_v), 2)


def test_passed(close: pd.Series, high: pd.Series, low: pd.Series, vol: pd.Series,
                cfg: dict | None = None) -> dict:
    """测试通过（VPA Test·拉升前最后一道工序）：近 look 日一次"缩量下探企稳·无供应"。

    下探日 = 收阴/探低（回踩），且 量缩到近 vol_n 日 dry_q 分位以下（无供应），
    且 收盘守住测试前箱底（未有效破位）。返回 {passed, days_ago, dry_pctile}。
    """
    c = cfg or _VPA
    close, high, low, vol = _clean(close), _clean(high), _clean(low), _clean(vol)
    look, vol_n = c["test_look"], c["test_vol_n"]
    if len(close) < vol_n + look + 2:
        return {"passed": False}
    box_low = float(low.tail(c["test_box_n"]).iloc[:-look].min())   # 测试前箱底（不含最近 look 日）
    vwin = vol.tail(vol_n)
    for a in range(1, look + 1):                                    # a=1 昨日 … look 最远
        i = -a
        dry = float((vwin <= float(vol.iloc[i])).mean())           # 该日量在近 vol_n 日的分位
        is_dip = (float(close.iloc[i]) <= float(close.iloc[i - 1])
                  or float(low.iloc[i]) <= float(low.iloc[i - 1]))
        hold = float(low.iloc[i]) >= box_low * 0.97 and float(close.iloc[i]) >= box_low
        if dry <= c["test_dry_q"] and is_dip and hold:
            return {"passed": True, "days_ago": a, "dry_pctile": round(dry, 2)}
    return {"passed": False}


def vpa_pre_markup(close: pd.Series, high: pd.Series, low: pd.Series, vol: pd.Series,
                   limit_mask: pd.Series | None = None, cfg: dict | None = None) -> dict:
    """VPA 拉升前临界评分（震仓 + 测试通过·吸筹尾声）。

    只在【底部区域 + 停止新低 + 非派发破位】前提下给分；核心=测试通过·无供应。
    返回 {score(0~100), critical(测试刚通过·近3日), phase, parts, pos}。客观结构·非买卖建议·point-in-time。
    """
    from app.factors.core import ma_slope
    c = cfg or _VPA
    close, high, low, vol = _clean(close), _clean(high), _clean(low), _clean(vol)
    if len(close) < c["min_bars"]:
        return {"score": 0, "critical": False, "phase": "—"}
    cur = float(close.iloc[-1])
    # ── 硬门槛：排除 派发 / 下跌中继 / 仍在跌 ──
    ll20, ll60 = float(low.tail(20).min()), float(low.tail(60).min())
    yr_hi, yr_lo = float(high.tail(250).max()), float(low.tail(250).min())
    pos = (cur - yr_lo) / (yr_hi - yr_lo) if yr_hi > yr_lo else 1.0
    if pos > c["pos_ceil"] or ll20 < ll60 * c["stop_tol"] or detect_double_top(close, high, vol):
        return {"score": 0, "critical": False, "phase": "—", "pos": round(pos, 2)}
    # ── 评分维度 ──
    sq = squeeze_pctile(high, low, close, 20, 250)
    sq = sq if sq is not None else 1.0
    age = box_age(high, low, 60)
    obv_sl = obv_slope_norm(close, vol, 20, limit_mask) or 0.0
    obv_dv = obv_divergence(close, vol, 20, limit_mask) or 0.0
    test = test_passed(close, high, low, vol, c)
    dshrink = down_day_shrink(close, vol, 40)
    ll20x = float(low.tail(20).iloc[:-1].min())               # 内联 Spring 判定(省去重算 obv/squeeze)
    vma20 = float(vol.tail(20).mean())
    is_spring = float(low.iloc[-1]) < ll20x and cur > ll20x and float(vol.iloc[-1]) < vma20
    ma20 = float(close.tail(20).mean())
    slope20 = ma_slope(close, 20)
    parts = {
        "蓄势收窄": round(max(0.0, (c["sq_good"] - min(sq, c["sq_good"])) / c["sq_good"]) * 12
                      + min(age, 40) / 40 * 8, 1),                       # 0~20
        "资金暗吸": round(min(1.0, max(0.0, obv_sl) * 40) * 13
                      + min(1.0, max(0.0, obv_dv) / 25) * 12, 1),        # 0~25
        "测试通过": round((22 + (1 - test.get("dry_pctile", 1)) * 8) if test.get("passed") else 0.0, 1),  # 0~30
        "下跌缩量": 15.0 if (dshrink and dshrink >= c["down_shrink_min"]) else (
            round(min(1.0, (dshrink or 0) / c["down_shrink_min"]) * 15, 1) if dshrink else 0.0),  # 0~15
        "微转强": round((5.0 if cur >= ma20 * 0.98 else 0.0) + (5.0 if slope20 >= -0.005 else 0.0), 1),  # 0~10
    }
    if is_spring:
        parts["测试通过"] = round(min(30.0, parts["测试通过"] + 6), 1)   # 震仓刚完成 加成
    score = round(sum(parts.values()))
    critical = bool(test.get("passed") and test.get("days_ago", 99) <= c["critical_days"]
                    and score >= c["critical_score"])
    phase = ("测试通过·临界" if critical else
             ("震仓Spring" if is_spring else ("吸筹候选" if score >= 50 else "—")))
    return {"score": score, "critical": critical, "phase": phase,
            "parts": parts, "pos": round(pos, 2), "test": test}


# ── LPS（Last Point of Support·放量突破后缩量回踩不破·威科夫最可靠买点）──────────
_LPS = {
    "sos_look": 20,        # SOS 突破发生在近 N 日内
    "box_n": 60,           # 突破前箱体回看
    "sos_up": 0.03,        # 突破日大阳 ≥ 3%
    "sos_vol_mult": 1.8,   # 突破日大量 ≥ 1.8×近20日均量
    "break_buf": 0.01,     # 有效突破：收盘 > 箱顶×(1+buf)
    "min_since": 2,        # 突破后至少 N 日（留出回踩）
    "max_since": 15,       # 突破后 N 日内（"刚回踩"·超了不算）
    "hold_buf": 0.03,      # 不破：回踩最低 ≥ 突破位×(1-buf)
    "pull_dry": 0.75,      # 缩量回踩：回踩段均量 < 突破日量×此值
    "near_buf": 0.06,      # 当前回落到突破位附近（不追高）
}


def lps_entry(close: pd.Series, high: pd.Series, low: pd.Series, vol: pd.Series,
              cfg: dict | None = None) -> dict:
    """LPS（放量突破后缩量回踩不破·威科夫最可靠买点）·point-in-time·客观结构·非买卖建议。

    近 sos_look 日内有 SOS（放量大阳突破前箱顶）→ 其后缩量回踩至突破位（前箱顶=现支撑）且未跌破
    = 突破确认·启动前临界。假突破（放量跌回箱内）→ is_lps=False。
    返回 {is_lps, days_since_sos, level, held, dry, near}。
    """
    c = cfg or _LPS
    close, high, low, vol = _clean(close), _clean(high), _clean(low), _clean(vol)
    n = len(close)
    if n < c["box_n"] + c["sos_look"] + 5:
        return {"is_lps": False}
    vma = vol.rolling(20).mean()
    sos_i = None
    level = 0.0
    for k in range(n - c["min_since"] - 1, n - c["max_since"] - 2, -1):   # 近→远找最近一次 SOS
        if k < c["box_n"]:
            break
        prior_top = float(high.iloc[k - c["box_n"]:k].max())             # k 日之前的箱顶（不含突破日）
        up = float(close.iloc[k]) / float(close.iloc[k - 1]) - 1
        vmk = float(vma.iloc[k]) if vma.iloc[k] == vma.iloc[k] else 0.0
        if (float(close.iloc[k]) > prior_top * (1 + c["break_buf"])
                and up >= c["sos_up"] and vmk > 0 and float(vol.iloc[k]) >= c["sos_vol_mult"] * vmk):
            sos_i, level = k, prior_top
            break
    if sos_i is None:
        return {"is_lps": False}
    since = n - 1 - sos_i
    pull_low = float(low.iloc[sos_i + 1:].min())
    pull_vol = float(vol.iloc[sos_i + 1:].mean())
    post_high = float(high.iloc[sos_i:].max())
    cur = float(close.iloc[-1])
    held = pull_low >= level * (1 - c["hold_buf"])                        # 不破：回踩守住突破位
    dry = pull_vol < float(vol.iloc[sos_i]) * c["pull_dry"]               # 缩量回踩（无供应）
    near = level * (1 - c["near_buf"]) <= cur <= post_high * 0.995        # 已回落到突破位附近·不追高
    return {"is_lps": bool(held and dry and near), "days_since_sos": since,
            "level": round(level, 2), "held": held, "dry": dry, "near": near}
