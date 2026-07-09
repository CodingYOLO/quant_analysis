"""
自选关键位速览：每只自选/持仓一行——均线价格(MA5/10/20/60) + 最近支撑/压力 + 量比 + 换手，一目了然。

复用现成件（不重造）：
  - `kline_loader.load_kline` 前复权日K → 均线价格
  - `key_levels.build_key_levels` → 可溯源支撑带/压力带（均线/前低前高/筹码密集区）
  - 幕数据实时快照(realtime_hub) → 现价/涨跌/量比(vol_ratio)/换手(turnover_rate)；未连时用 EOD 兜底

性能：EOD 稳定部分（均线/支撑压力/换手/量比）**按日按股缓存**（首次由夜间 warmup 预热），
每次请求只做「读缓存 + 实时快照叠加」→ 秒开。纯客观"位"描述·非买卖建议。
"""

from __future__ import annotations

import datetime
import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.kline_loader import load_kline
from app.strategy.key_levels import build_key_levels

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 260   # 覆盖 MA60 + 前低前高(60日) + key_levels


def build_watch_levels(provider: CompositeProvider | None = None) -> dict:
    """构建自选关键位速览（缓存EOD稳定层 + 实时叠加）。持仓在前、自选在后。"""
    provider = provider or CompositeProvider()
    from app.strategy.realtime_hub import snapshot, stock_df, watch_meta
    meta = watch_meta()
    if not meta:
        return {"ok": True, "date": "", "live": False, "rows": []}
    dbmap, date = _settled_basic_map(provider)
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=int(_LOOKBACK_DAYS * 1.6))).strftime("%Y%m%d")
    stable = _stable_layer(provider, date, list(meta.keys()), dbmap, start, end)
    live = _live_map(snapshot, stock_df)
    rows = [_overlay(code, m, stable[code], live.get(code))
            for code, m in meta.items() if code in stable]
    rows.sort(key=lambda r: (not r["is_holding"], r["name"]))
    return {"ok": True, "date": date, "live": bool(live), "rows": rows}


def _overlay(code: str, m: dict, s: dict, q: dict | None) -> dict:
    """稳定层 + 实时快照(或EOD)叠加 → 前端行。支撑/压力距离按当前价重算。"""
    price = _num(q and q.get("price")) or s["eod_close"]
    pct = _num(q and q.get("pct_chg"))
    if pct is None:
        pct = s.get("eod_pct")
    return {
        "code": code[:6], "name": m.get("name") or code, "is_holding": bool(m.get("is_holding")),
        "price": round(price, 2), "pct_chg": pct,
        "ma5": s["ma5"], "ma10": s["ma10"], "ma20": s["ma20"], "ma60": s["ma60"],
        "vol_ratio": _num(q and q.get("vol_ratio")) or s.get("eod_vol_ratio"),
        "turnover": _num(q and q.get("turnover_rate")) or s.get("eod_turnover"),
        "support": _rel(s.get("sup_price"), price),
        "resistance": _rel(s.get("res_price"), price),
        "stop_loss": _num(m.get("stop_loss")),
    }


def _rel(level_price: float | None, price: float) -> dict | None:
    """某关键位价格 + 距当前价%（负=下方，正=上方）。"""
    if not level_price or not price:
        return None
    return {"price": round(level_price, 2), "dist": round((level_price / price - 1) * 100, 1)}


# ── EOD 稳定层（慢·按日缓存整份 {code:稳定}·只补算缺的·夜间预热）──────────────
def _stable_layer(provider: CompositeProvider, date: str, codes: list,
                  dbmap: dict, start: str, end: str) -> dict:
    """{code: 稳定层} 按日 JSON 缓存；新增自选只补算缺的那只（不重跑全部）。"""
    from app.strategy import detail_common as DC
    path = DC.cache_path("watch_levels", date, "stable")
    cached = DC.load_cache(path) or {}
    changed = False
    for code in codes:
        if code not in cached:
            s = _compute_stable(code, provider, dbmap.get(code), start, end)
            if s:
                cached[code] = s
                changed = True
    if changed:
        DC.save_cache(path, cached)
    return cached


def _compute_stable(code: str, provider: CompositeProvider, db: object, start: str, end: str) -> dict | None:
    """单只EOD稳定层：均线价格 + 最近支撑/压力价 + EOD收盘/涨跌/量比/换手。数据不足→None。"""
    k = load_kline(code, start, end, provider, adj="qfq")
    if k.empty or len(k) < 60:
        return None
    close = k["close"].astype(float)
    eod_close = round(float(close.iloc[-1]), 2)

    def ma(n: int) -> float | None:
        return round(float(close.tail(n).mean()), 2) if len(close) >= n else None

    lv = build_key_levels(k) or {}
    sup = (lv.get("support") or [None])[0]
    res = (lv.get("resistance") or [None])[0]
    eod_vr = _num(db.get("volume_ratio")) if db is not None else None
    if eod_vr is None:                                   # daily_basic 无量比→用日K算(今量/前5日均量)
        vol = pd.to_numeric(k["vol"], errors="coerce")
        base = float(vol.iloc[-6:-1].mean()) if len(vol) >= 6 else 0.0
        eod_vr = round(float(vol.iloc[-1]) / base, 2) if base else None
    return {
        "ma5": ma(5), "ma10": ma(10), "ma20": ma(20), "ma60": ma(60),
        "sup_price": sup.get("mid") if isinstance(sup, dict) else None,
        "res_price": res.get("mid") if isinstance(res, dict) else None,
        "eod_close": eod_close,
        "eod_pct": round(float(k["pct_chg"].iloc[-1]), 2) if "pct_chg" in k.columns and pd.notna(k["pct_chg"].iloc[-1]) else None,
        "eod_vol_ratio": eod_vr,
        "eod_turnover": _num(db.get("turnover_rate")) if db is not None else None,
    }


# ── 数据装配 ────────────────────────────────────────────────────────────────
def _live_map(snapshot, stock_df) -> dict:
    """幕数据快照已连(>500只)→ {code: 实时行(price/pct_chg/vol_ratio/turnover_rate)}·否则空。"""
    try:
        if snapshot().count() > 500:
            return {r["ts_code"]: r for r in stock_df().to_dict("records")}
    except Exception as e:
        logger.debug("[自选关键位] 实时快照取用失败: %s", e)
    return {}


def _settled_basic_map(provider: CompositeProvider) -> tuple[dict, str]:
    """最近已结算交易日的 daily_basic（换手/量比 EOD 兜底）。盘中今日未结算→回退上一日。"""
    from app.nodes.quick_report import _recent_trade_dates
    today = datetime.date.today().strftime("%Y%m%d")
    for d in reversed(_recent_trade_dates(provider, today, 3) or [today]):
        try:
            db = provider.get_daily_basic(d)
        except Exception:
            db = None
        if db is not None and not db.empty:
            return {str(r["ts_code"]): r for _, r in db.iterrows()}, d
    return {}, today


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, 2) if v == v else None
