"""
威科夫阶段 T+1/T+3/T+5 前向收益回测（诚实验证：各阶段 vs 基准有没有增量）。

口径与系统一致（复用 signal_eval）：买入=信号日次日(T+1)开盘·t{N}=T+N收盘相对T+1开盘。
阶段在每个信号日**point-in-time** 计算（只用 ≤d 数据·涨跌停量能剔除）。
诚实注意：T+1开盘买入是**乐观口径**——SOS突破若次日一字板涨停实际买不到（用 gap 列体现跳空幅度）。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.backtest.signal_eval import (_agg, _dates_with_forward, _forward_returns,
                                       _make_daily_loader)
from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix
from app.factors import wyckoff as W
from app.nodes.quick_report import _board_limit_pct

logger = logging.getLogger(__name__)

_PHASES = ("吸筹候选", "Spring", "SOS突破", "派发破位")


def evaluate_wyckoff_phases(end: str, window: int = 25, min_circ_yi: float = 80.0) -> dict:
    """回测：信号窗口内每交易日算全(液)市场威科夫阶段，按阶段桶 T+1/T+3/T+5 收益 + 基准。

    end: 最新数据交易日(YYYYMMDD)。window: 回测信号日数(会自动只取有T+5前向的)。
    min_circ_yi: 流通市值下限(亿)·过滤微盘噪音。
    """
    provider = CompositeProvider()
    # 载一次全市场矩阵（信号窗口 + 260 日蓄势/形态回看）
    close_m, open_m, high_m, low_m, vol_m = load_price_matrix(end, provider, n_days=window + 270)
    idx = list(close_m.index)                                  # 升序交易日
    dates_ext = _dates_with_forward(provider, idx[0], end)     # 含 end 之后的前向日
    get_daily = _make_daily_loader(provider)

    universe = _liquid_universe(provider, end, min_circ_yi, set(close_m.columns))
    names = _name_map(provider)
    idx_close = _index_close(provider, end)                    # 上证收盘(判 regime·point-in-time)
    sig_dates = [d for d in idx[-window:] if d in dates_ext]   # 信号日
    logger.info("[威科夫回测] 信号日 %d · universe %d 只", len(sig_dates), len(universe))

    _REG = ("牛", "震荡", "熊")
    buckets = {p: {"t1": [], "t3": [], "t5": [], "gap": []} for p in (*_PHASES, "基准(全液)")}
    # regime 分档：只拆 SOS突破 与 基准（验证突破的状态依赖·prompt 硬性要求）
    reg_b = {r: {p: {"t1": [], "t3": [], "t5": [], "gap": []} for p in ("SOS突破", "基准(全液)")} for r in _REG}
    reg_count = {r: 0 for r in _REG}
    for d in sig_dates:
        pos = idx.index(d)
        d_ext = dates_ext.index(d)
        reg = _regime_at(idx_close, d)
        reg_count[reg] += 1
        for code in universe:
            ph = _phase_at(code, pos, close_m, high_m, low_m, vol_m, names.get(code, ""))
            fwd = _forward_returns(code, d_ext, dates_ext, get_daily)
            if not fwd:
                continue
            _push(buckets["基准(全液)"], fwd)
            _push(reg_b[reg]["基准(全液)"], fwd)
            if ph in _PHASES:
                _push(buckets[ph], fwd)
            if ph == "SOS突破":
                _push(reg_b[reg]["SOS突破"], fwd)

    return {
        "ok": True, "end": end, "window": len(sig_dates), "min_circ_yi": min_circ_yi,
        "universe": len(universe), "regime_days": reg_count,
        "phases": {p: _agg_all(buckets[p]) for p in (*_PHASES, "基准(全液)")},
        "by_regime": {r: {p: _agg_all(reg_b[r][p]) for p in ("SOS突破", "基准(全液)")} for r in _REG},
        "note": ("买入=次日T+1开盘·t{N}=T+N收盘相对T+1开盘(系统口径)。阶段 point-in-time·涨停量能已剔。"
                 "⚠️gap=次日跳空%(SOS若gap大=一字板买不到·收益偏乐观)。小样本(n<30)不足信。"
                 "regime 分档验证突破的状态依赖(A股:震荡有效/牛市反转/熊市失效)。"),
    }


def _index_close(provider, end: str) -> pd.Series:
    """上证收盘序列(index=trade_date str·升序)·判市场 regime 用。"""
    df = provider.get_index_daily("000001.SH", end)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    df = df.sort_values("trade_date")
    return pd.Series(pd.to_numeric(df["close"], errors="coerce").values,
                     index=df["trade_date"].astype(str).values)


def _regime_at(idx_close: pd.Series, d: str) -> str:
    """信号日 d 的市场状态（只用 ≤d 数据）：牛/震荡/熊。上证 MA20/MA60 结构 + 20日动量。"""
    s = idx_close[idx_close.index <= d]
    if len(s) < 61:
        return "震荡"
    c = float(s.iloc[-1])
    ma20, ma60 = float(s.tail(20).mean()), float(s.tail(60).mean())
    ma20_prev = float(s.iloc[-25:-5].mean())               # 5日前的 MA20
    ret20 = (c / float(s.iloc[-21]) - 1) * 100
    if c > ma20 > ma60 and ma20 > ma20_prev and ret20 > 3:
        return "牛"
    if c < ma60 and ma20 < ma20_prev:
        return "熊"
    return "震荡"


def _phase_at(code, pos, close_m, high_m, low_m, vol_m, name) -> str:
    """信号日(pos)当天的威科夫阶段（只用 ≤pos 的数据·point-in-time）。"""
    try:
        s = pd.to_numeric(close_m[code].iloc[:pos + 1], errors="coerce").dropna()
        if len(s) < 60:
            return "—"
        hi = pd.to_numeric(high_m[code], errors="coerce").reindex(s.index)
        lo = pd.to_numeric(low_m[code], errors="coerce").reindex(s.index)
        v = pd.to_numeric(vol_m[code], errors="coerce").reindex(s.index)
        lim = _board_limit_pct(code, name)
        pct = s.pct_change() * 100
        mask = (pct >= lim - 0.3) | (pct <= -(lim - 0.3))
        return W.wyckoff_phase(s, hi, lo, v, mask)
    except Exception:
        return "—"


def _push(b: dict, fwd: dict) -> None:
    for k in ("t1", "t3", "t5"):
        if fwd.get(k) is not None:
            b[k].append(fwd[k])
    if fwd.get("gap") is not None:
        b["gap"].append(fwd["gap"])


def _agg_all(b: dict) -> dict:
    out = {k: _agg(b[k]) for k in ("t1", "t3", "t5")}
    out["avg_gap"] = round(sum(b["gap"]) / len(b["gap"]), 2) if b["gap"] else None
    return out


def _liquid_universe(provider, end, min_circ_yi, cols: set) -> list[str]:
    dbf = provider.get_daily_basic(end)
    if dbf is None or dbf.empty:
        return [c for c in cols]
    cmv = pd.to_numeric(dbf.set_index("ts_code")["circ_mv"], errors="coerce") / 1e4
    keep = set(cmv[cmv >= min_circ_yi].index)
    return [c for c in cols if c in keep]


def _name_map(provider) -> dict:
    try:
        sb = provider.get_stock_basic()
        return dict(zip(sb["ts_code"], sb["name"]))
    except Exception:
        return {}
