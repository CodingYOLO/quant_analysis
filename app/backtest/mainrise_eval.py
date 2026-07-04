"""
强势主升浪·平台突破 技术条件 T+1/5/10 前向收益回测（诚实验证博主"满足越多概率越高"）。

口径与系统一致（复用 signal_eval / wyckoff_eval）：买入=信号日次日(T+1)开盘·t{N}=T+N收盘相对T+1开盘。
每个信号日 **point-in-time** 计算技术条件（只用 ≤d 数据）。

验证目标：
  1. **单调性**——满足的技术条件越多，T+N 超额(vs 全液基准)是否越高？
  2. **边际贡献**——每个条件单独命中 vs 未命中的收益差（哪个条件真带 alpha、哪个是噪音/共线）。

诚实边界：
  - 只回测**能从K线重建的 6 个技术条件**(平台突破/MA60向上/MACD零轴金叉/RSI>50/20日量比≥2/无假突破)。
  - 「主力连续流入」「板块走强」需逐日重建历史 moneyflow / 全市场 RPS（很重）→**未纳入本回测**，
    是叠加在技术面之上的过滤，需另做（不在此假装已验证）。
  - T+1 开盘买入是乐观口径（突破次日一字板买不到）；小样本(n<30)不足信。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.backtest.signal_eval import _agg, _dates_with_forward, _make_daily_loader
from app.backtest.wyckoff_eval import _liquid_universe
from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix
from app.factors import core as F
from app.factors import wyckoff as W
from app.factors.patterns.price_volume import PlatformBreakout

logger = logging.getLogger(__name__)

_HZ = (1, 5, 10)
_CRIT = ("平台突破", "MA60向上", "MACD零轴金叉", "RSI>50", "20日量比≥2", "无假突破")
_PB = PlatformBreakout()          # 默认 15 日平台突破（放量+窄幅+破箱顶）


def evaluate_mainrise(end: str, window: int = 30, min_circ_yi: float = 100.0) -> dict:
    """回测信号窗口内全（液）市场每日主升浪技术条件·按满足数分桶 T+1/5/10 收益 + 各条件边际。"""
    provider = CompositeProvider()
    close_m, open_m, high_m, low_m, vol_m = load_price_matrix(end, provider, n_days=window + 130)
    idx = list(close_m.index)
    dates_ext = _dates_with_forward(provider, idx[0], end)
    get_daily = _make_daily_loader(provider)
    universe = _liquid_universe(provider, end, min_circ_yi, set(close_m.columns))
    sig_dates = [d for d in idx[-window:] if d in dates_ext]
    logger.info("[主升浪回测] 信号日 %d · universe %d 只", len(sig_dates), len(universe))

    base = {h: [] for h in _HZ}
    by_count = {n: {h: [] for h in _HZ} for n in range(7)}          # 满足 0..6 条
    by_thresh = {k: {h: [] for h in _HZ} for k in ("≥3", "≥4", "≥5", "=6", "仅平台突破")}
    by_crit = {c: {"命中": {h: [] for h in _HZ}, "未命中": {h: [] for h in _HZ}} for c in _CRIT}
    n_eval = 0

    for d in sig_dates:
        pos, d_ext = idx.index(d), dates_ext.index(d)
        for code in universe:
            crit = _criteria(code, pos, close_m, open_m, high_m, low_m, vol_m)
            if crit is None:
                continue
            fwd = _fwd(code, d_ext, dates_ext, get_daily)
            if not fwd:
                continue
            n_eval += 1
            _push(base, fwd)
            n = sum(crit.values())
            _push(by_count[n], fwd)
            for k, t in (("≥3", 3), ("≥4", 4), ("≥5", 5), ("=6", 6)):
                if n >= t if k != "=6" else n == 6:
                    _push(by_thresh[k], fwd)
            if crit["平台突破"]:
                _push(by_thresh["仅平台突破"], fwd)
            for c, hit in crit.items():
                _push(by_crit[c]["命中" if hit else "未命中"], fwd)

    base_agg = {f"t{h}": _agg(base[h]) for h in _HZ}
    base_mean = {h: (base_agg[f"t{h}"]["mean"] or 0.0) for h in _HZ}

    def _excess(b):
        agg = {f"t{h}": _agg(b[h]) for h in _HZ}
        for h in _HZ:
            m = agg[f"t{h}"]["mean"]
            agg[f"t{h}"]["excess"] = round(m - base_mean[h], 2) if m is not None else None
        return agg

    return {
        "ok": True, "end": end, "window": len(sig_dates), "universe": len(universe),
        "n_eval": n_eval, "criteria": list(_CRIT),
        "baseline": base_agg,
        "by_count": {n: _excess(by_count[n]) for n in range(7)},        # 满足N条→超额(看单调性)
        "by_threshold": {k: _excess(by_thresh[k]) for k in by_thresh},
        "by_criterion": {c: {"命中": _excess(by_crit[c]["命中"]),
                             "未命中": _excess(by_crit[c]["未命中"])} for c in _CRIT},
        "note": ("买入=次日T+1开盘·t{N}=T+N收盘相对T+1开盘(乐观口径·突破次日一字板买不到)。"
                 "只回测可从K线重建的6个技术条件·**资金连续流入/板块走强未纳入**(需另做)。"
                 "excess=该桶均值−全液基准均值。小样本(n<30)不足信。单调性看 by_count 的 excess 是否随满足数上升。"),
    }


def _criteria(code, pos, close_m, open_m, high_m, low_m, vol_m) -> dict | None:
    """信号日(pos)当天的 6 个主升浪技术条件（只用 ≤pos 数据·point-in-time）。数据不足→None。"""
    try:
        s = pd.to_numeric(close_m[code].iloc[:pos + 1], errors="coerce").dropna()
        if len(s) < 65:
            return None
        hi = pd.to_numeric(high_m[code], errors="coerce").reindex(s.index)
        lo = pd.to_numeric(low_m[code], errors="coerce").reindex(s.index)
        op = pd.to_numeric(open_m[code], errors="coerce").reindex(s.index)
        v = pd.to_numeric(vol_m[code], errors="coerce").reindex(s.index)
        o = pd.DataFrame({"open": op, "high": hi, "low": lo, "close": s, "vol": v}).dropna()
        if len(o) < 65:
            return None
        vv = v.dropna()
        return {
            "平台突破": bool(_PB.detect(o)),
            "MA60向上": bool(float(s.tail(60).mean()) > float(s.iloc[-65:-5].mean())),
            "MACD零轴金叉": bool(F.macd_golden_cross(s)),          # 内部已含"零轴附近"约束
            "RSI>50": bool(float(F.rsi(s, 14).iloc[-1]) > 50),
            "20日量比≥2": bool(len(vv) >= 21 and F.volume_ratio(vv, 20) >= 2),
            "无假突破": bool(not W.false_breakout(s, hi, 60)),
        }
    except Exception:
        return None


def _fwd(code, d_idx, dates, get_daily) -> dict | None:
    """T+1开盘买入·T+{1,5,10}收盘相对买入价（同系统口径）。"""
    if d_idx + 1 >= len(dates):
        return None
    t1 = get_daily(dates[d_idx + 1])
    if t1 is None or code not in t1.index:
        return None
    entry = float(t1.loc[code, "open"])
    if entry <= 0:
        return None
    out = {}
    for h in _HZ:
        j = d_idx + h
        dfh = get_daily(dates[j]) if j < len(dates) else None
        if dfh is None or code not in dfh.index:
            out[h] = None
            continue
        ex = float(dfh.loc[code, "close"])
        out[h] = round((ex - entry) / entry * 100, 3) if ex > 0 else None
    return out


def _push(b: dict, fwd: dict) -> None:
    for h in _HZ:
        if fwd.get(h) is not None:
            b[h].append(fwd[h])
