"""大盘指数监控：关键指数实时报价 + 日/周/月 K 线（历史 + 当日实时最新一根）。

数据源：实时报价走 `get_realtime_quote`(新浪·指数个股通用)；K线历史走 `get_index_daily_range`(Tushare)，
周/月复用 `stock_profile._resample_ohlc`，蜡烛/量/均线复用 `_kline_payload` + 前端 `AKline.render`。
盯盘页大盘一览用。纯展示·非买卖建议。
"""

from __future__ import annotations

import datetime
import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.strategy.stock_profile import _kline_payload, _resample_ohlc

logger = logging.getLogger(__name__)

# 关键指数（大盘/成长/硬科技/核心/小盘/北交所·覆盖科技波段常看的几条）
INDICES: list[tuple[str, str]] = [
    ("000001.SH", "上证指数"), ("399001.SZ", "深证成指"), ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"), ("000300.SH", "沪深300"), ("000852.SH", "中证1000"),
    ("899050.BJ", "北证50"),
]
_CODE2NAME = dict(INDICES)


def _ff(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def index_quotes(provider: CompositeProvider | None = None) -> list[dict]:
    """关键指数实时报价（现价/涨跌%/涨跌额/今日OHLC/成交额亿）。空→[]。"""
    prov = provider or CompositeProvider()
    try:
        df = prov.get_realtime_quote([c for c, _ in INDICES])
    except Exception as e:
        logger.warning("[指数] 实时报价失败: %s", e)
        return []
    if df is None or df.empty:
        return []
    by = {str(r["ts_code"]): r for _, r in df.iterrows()}
    out = []
    for code, name in INDICES:
        r = by.get(code)
        if r is None:
            continue
        price, prev = _ff(r.get("price")), _ff(r.get("prev_close"))
        out.append({
            "code": code, "name": name, "price": round(price, 2),
            "pct_chg": round(_ff(r.get("pct_chg")), 2),
            "chg": round(price - prev, 2) if prev else 0.0,
            "amount_yi": round(_ff(r.get("amount")) / 1e8, 1),
            "open": round(_ff(r.get("open")), 2), "high": round(_ff(r.get("high")), 2),
            "low": round(_ff(r.get("low")), 2), "prev": round(prev, 2),
        })
    return out


def index_kline(code: str, freq: str = "D", provider: CompositeProvider | None = None,
                bars: int = 120) -> dict:
    """指数 K 线（freq: D日/W周/M月）·历史日线 + 当日实时最新一根（盘中未入库时补）。

    返回 `_kline_payload` 格式（dates/candle[o,c,l,h]/vol/ma5/10/20/60）+ code/name/freq。
    """
    prov = provider or CompositeProvider()
    look = {"D": 220, "W": 900, "M": 2400}.get(freq, 220)          # 日历日·月线需更长历史
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=look)).strftime("%Y%m%d")
    try:
        d = prov.get_index_daily_range(code, start, end)
    except Exception as e:
        logger.warning("[指数K线] %s 历史失败: %s", code, e)
        return {}
    if d is None or d.empty:
        return {}
    cols = [c for c in ("trade_date", "open", "high", "low", "close", "vol") if c in d.columns]
    d = d[cols].sort_values("trade_date").reset_index(drop=True)
    if "vol" not in d.columns:
        d["vol"] = 0
    d = _append_live_bar(prov, code, d, end)                       # 盘中补当日实时一根
    if freq == "W":
        d = _resample_ohlc(d, "W-FRI")
    elif freq == "M":
        d = _resample_ohlc(d, "ME")
    payload = _kline_payload(d.tail(bars))
    payload.update({"code": code, "name": _CODE2NAME.get(code, code), "freq": freq})
    return payload


def _append_live_bar(prov: CompositeProvider, code: str, d: pd.DataFrame, today: str) -> pd.DataFrame:
    """当日日线未入库(盘中)时·用实时报价补一根今日K(vol暂0·仅让最新一根实时可见)。"""
    if not d.empty and str(d["trade_date"].iloc[-1]) == today:
        return d                                                   # 当日已入库·无需补
    try:
        q = prov.get_realtime_quote([code])
    except Exception:
        return d
    if q is None or q.empty:
        return d
    r = q.iloc[0]
    price, open_p = _ff(r.get("price")), _ff(r.get("open"))
    if not (price and open_p):
        return d
    live = {"trade_date": today, "open": open_p, "high": _ff(r.get("high")),
            "low": _ff(r.get("low")), "close": price, "vol": 0}
    return pd.concat([d, pd.DataFrame([live])], ignore_index=True)
