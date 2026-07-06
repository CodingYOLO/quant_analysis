"""
S1：选股池信号库（个股级，全复用 app.factors）。

为全市场（经基础过滤）每只股票计算一张「信号表」，供 stock_pool 的策略引擎判定。
信号定义见《选股池设计文档》§2（精确、可算、无模糊）：
  站上MA20/60、MA60斜率>0.5%、突破(创20日新高 或 MACD金叉+量比≥1.2)、
  缩量回踩(距MA20≤3% + 量比<0.8 + pullback_score≥50)、反转形态、人气代理排名。

口径：个股信号用 Tushare 不复权（与现有选股线一致，且 pullback 需同口径 open/low/close）；
板块广度才用前复权。单位：金额统一折算到「亿元」。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix
from app.factors.breadth_qfq import _recent_trade_dates
from app.factors.core import (
    calc_buy_zones,
    calc_rps,
    calc_stop_loss_price,
    calc_take_profit_prices,
    has_lower_shadow,
    macd_golden_cross,
    pullback_quality_score,
    volume_ratio,
)

logger = logging.getLogger(__name__)

# 阈值（《设计文档》§P2/P5，可校准）
_MA60_SLOPE_LOOKBACK = 5       # MA60 斜率回看交易日
_MA60_SLOPE_MIN = 0.5         # %
_NEW_HIGH_WIN = 20
_VOL_RATIO_BREAK = 1.2        # 突破放量量比下限
_NEAR_MA20 = 0.03             # 距 MA20 ±3%
_SHRINK_VOL_RATIO = 0.8       # 缩量量比上限
_PULLBACK_MIN = 50.0
_CHANGE_7D_LOW = 5.0          # 反转：7 日涨幅上限
_POP_RANK_MAX = 1500          # 人气代理：换手率排名上限


def build_signal_table(
    trade_date: str,
    provider: CompositeProvider | None = None,
    lookback: int = 70,
    min_cap_yi: float = 200.0,
    max_cap_yi: float = 5000.0,
    min_amount_yi: float = 1.0,
) -> pd.DataFrame:
    """
    计算全市场（基础过滤后）个股信号表。

    Returns:
        DataFrame（index=ts_code），含信号布尔位 + 关键因子。空数据返回空表。
    """
    provider = provider or CompositeProvider()
    close_m, open_m, _high_m, low_m, vol_m = load_price_matrix(trade_date, provider, n_days=lookback)
    if close_m is None or close_m.empty:
        return pd.DataFrame()

    daily = provider.get_daily(trade_date)
    db = provider.get_daily_basic(trade_date)
    sb = provider.get_stock_basic()
    if any(x is None or x.empty for x in (daily, db, sb)):
        return pd.DataFrame()

    # ---- 今日截面 + 基础过滤 ----
    d = daily[["ts_code", "close", "open", "low", "pct_chg", "amount"]].copy()
    d = d.merge(db[["ts_code", "turnover_rate", "circ_mv"]], on="ts_code", how="left")
    d = d.merge(sb[["ts_code", "name", "industry"]], on="ts_code", how="left")
    d["amount_yi"] = pd.to_numeric(d["amount"], errors="coerce") / 1e5      # 千元→亿
    d["circ_mv_yi"] = pd.to_numeric(d["circ_mv"], errors="coerce") / 1e4    # 万元→亿
    d["pct_chg"] = pd.to_numeric(d["pct_chg"], errors="coerce")
    d["turnover"] = pd.to_numeric(d["turnover_rate"], errors="coerce")

    base = d[
        (~d["name"].fillna("").str.contains("ST"))
        & (d["circ_mv_yi"] >= min_cap_yi) & (d["circ_mv_yi"] <= max_cap_yi)
        & (d["amount_yi"] >= min_amount_yi)
        & (d["pct_chg"].abs() <= 9.3)
    ].copy()
    if base.empty:
        return pd.DataFrame()

    # ---- 全市场辅助：RPS / 主力3日 / 换手排名 ----
    rps = calc_rps(close_m, 50)                                   # Series by ts_code
    main_flow_3d = _main_flow_3d(provider, trade_date)            # {ts_code: 亿}
    d["_tr_rank"] = d["turnover"].rank(ascending=False, method="min")
    turnover_rank = dict(zip(d["ts_code"], d["_tr_rank"]))

    rows = []
    for r in base.itertuples(index=False):
        ts = r.ts_code
        if ts not in close_m.columns:
            continue
        close = close_m[ts].dropna()
        if len(close) < 65:
            continue
        sig = _stock_signals(
            close=close,
            vol=vol_m[ts].dropna() if ts in vol_m.columns else pd.Series(dtype=float),
            open_=open_m[ts].dropna() if ts in open_m.columns else pd.Series(dtype=float),
            low=low_m[ts].dropna() if ts in low_m.columns else pd.Series(dtype=float),
            today_open=float(r.open), today_low=float(r.low), today_close=float(r.close),
        )
        sig.update({
            "ts_code": ts, "name": r.name, "industry": r.industry,
            "pct_chg": round(float(r.pct_chg), 2), "turnover": round(float(r.turnover or 0), 2),
            "amount_yi": round(float(r.amount_yi), 2), "circ_mv_yi": round(float(r.circ_mv_yi), 1),
            "main_flow_3d": round(main_flow_3d.get(ts, 0.0), 2),
            "rps50": round(float(rps.get(ts, 0.0)), 1),
            "turnover_rank": int(turnover_rank.get(ts, 99999)),
            "popular": turnover_rank.get(ts, 99999) <= _POP_RANK_MAX,
        })
        rows.append(sig)

    return pd.DataFrame(rows).set_index("ts_code") if rows else pd.DataFrame()


def _stock_signals(close, vol, open_, low, today_open, today_low, today_close) -> dict:
    """单只股票的信号位（close 等为按日期升序的不复权序列）。"""
    cur = float(close.iloc[-1])
    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())
    above5 = cur >= ma5
    above10 = cur >= ma10
    above20 = cur >= ma20
    above60 = cur >= ma60
    ma_bull_short = ma5 > ma10 > ma20   # 短期多头排列（短线核心结构）

    # MA60 斜率（较 N 日前）
    ma60_prev = float(close.tail(60 + _MA60_SLOPE_LOOKBACK).head(60).mean()) if len(close) >= 60 + _MA60_SLOPE_LOOKBACK else ma60
    ma60_slope = (ma60 - ma60_prev) / ma60_prev * 100 if ma60_prev > 0 else 0.0
    slope_up = ma60_slope > _MA60_SLOPE_MIN

    new_high20 = cur >= float(close.tail(_NEW_HIGH_WIN).max())
    macd_gold = macd_golden_cross(close)
    vr = volume_ratio(vol, 5) if len(vol) >= 6 else 0.0
    breakout = new_high20 or (macd_gold and vr >= _VOL_RATIO_BREAK)

    near_ma20 = ma20 > 0 and abs(cur - ma20) / ma20 <= _NEAR_MA20
    try:
        pb_score = pullback_quality_score(close, vol, open_, low)
    except Exception:
        pb_score = 0.0
    is_pullback = near_ma20 and vr < _SHRINK_VOL_RATIO and pb_score >= _PULLBACK_MIN

    change_7d = (cur / float(close.iloc[-8]) - 1) * 100 if len(close) >= 8 else 0.0
    rev_form = has_lower_shadow(today_open, today_low, today_close) or (today_close > today_open)

    # 风险/位置（供选股池重点分做风险调整：过热、逼近历史高位）
    bias20 = (cur - ma20) / ma20 * 100 if ma20 > 0 else 0.0   # 20日乖离率：远离均线=过热/回归风险
    high120 = float(close.tail(120).max())                     # 近120日高
    dist_high = (cur / high120 - 1) * 100 if high120 > 0 else 0.0  # 距高点(≤0，越近0越高位)

    # 交易计划价位（基于序列，仓位由 stock_pool 按大盘状态另算）
    try:
        buy_low, buy_high = calc_buy_zones(close, vol)
        stop = calc_stop_loss_price(close)
        tp1, tp2 = calc_take_profit_prices(close)
    except Exception:
        buy_low = buy_high = stop = tp1 = tp2 = 0.0

    return {
        "buy_low": round(buy_low, 2), "buy_high": round(buy_high, 2),
        "stop_loss": round(stop, 2), "take_profit_1": round(tp1, 2), "take_profit_2": round(tp2, 2),
        "close": round(cur, 2), "ma5": round(ma5, 2), "ma10": round(ma10, 2),
        "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "above_ma5": bool(above5), "above_ma10": bool(above10),
        "above_ma20": bool(above20), "above_ma60": bool(above60),
        "ma_bull_short": bool(ma_bull_short),
        "ma60_slope": round(ma60_slope, 2), "slope_up": bool(slope_up),
        "new_high20": bool(new_high20), "macd_gold": bool(macd_gold), "vol_ratio": round(vr, 2),
        "breakout": bool(breakout), "near_ma20": bool(near_ma20),
        "pullback_score": round(pb_score, 1), "is_pullback": bool(is_pullback),
        "change_7d": round(change_7d, 2), "rev_form": bool(rev_form),
        "bias20": round(bias20, 2), "dist_high": round(dist_high, 2),
    }


def _main_flow_3d(provider: CompositeProvider, trade_date: str) -> dict:
    """近 3 个交易日主力净流入合计（亿元），{ts_code: 亿}·超大单+大单(东财/同花顺口径)。"""
    from app.data.moneyflow import main_net_wan
    out: dict[str, float] = {}
    try:
        dates = _recent_trade_dates(provider, trade_date, 3)
    except Exception:
        dates = [trade_date]
    for d in dates:
        try:
            mf = provider.get_money_flow(d)
        except Exception:
            continue
        net = main_net_wan(mf)                                 # 主力净(万元)·超大单+大单
        for ts, v in net.items():
            if pd.notna(v):
                out[ts] = out.get(ts, 0.0) + float(v) / 1e4   # 万元→亿
    return out
