"""大盘量能：今日实时两市成交额 vs 昨日/近5日 → 放量/缩量。

盘中「今日累计」不能直接比「昨日全天」(会永远显示缩量)→ 必须时段归一 / 同时段对比：
- **时段归一(近似)**：今累计÷已开盘分钟 ÷ (5日全天均÷240)·预估全天。早盘成交量U型分布(开盘尾盘重)→有偏差·标"近似"。
- **同时段(精确·Phase2)**：记录大盘分时Σamount曲线·攒≥3日→今累计 vs 过去5日同一时刻累计·对齐东财/同花顺量比真口径·自动去"近似"标签。

数据源：今日=Σ幕数据快照 amount(沪深两市·**元**·已实测) · 昨/5日=Tushare 日线 amount(千元→元)。
沪深口径(不含北交所·与东财"两市成交额"一致)。
"""

from __future__ import annotations

import datetime
import json
import logging

import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

_TRADE_MIN = 240                # 全天连续竞价分钟(9:30-11:30 + 13:00-15:00)
_prov_singleton: CompositeProvider | None = None


def _prov() -> CompositeProvider:
    global _prov_singleton
    if _prov_singleton is None:
        _prov_singleton = CompositeProvider()
    return _prov_singleton


def _elapsed_trade_minutes(now: datetime.datetime) -> int:
    """已开盘的连续竞价分钟数(0-240·扣午休)。"""
    hm = now.hour * 60 + now.minute
    m = 0
    if hm > 570:                                     # 9:30
        m += min(hm, 690) - 570                       # 至 11:30
    if hm > 780:                                     # 13:00
        m += min(hm, 900) - 780                       # 至 15:00
    return max(0, min(m, _TRADE_MIN))


_BASE = {"date": None, "prev": 0.0, "avg5": 0.0}


def _baseline(today: str) -> tuple[float, float]:
    """(昨日, 近5日均) 两市全天成交额(元)·按交易日缓存(每日只算一次)。"""
    if _BASE["date"] == today:
        return _BASE["prev"], _BASE["avg5"]
    from app.nodes.quick_report import _recent_trade_dates
    prov = _prov()
    ds = [d for d in _recent_trade_dates(prov, today, 7) if d < today][-5:]
    tots = []
    for d in ds:
        dl = prov.get_daily(d)
        if dl is not None and not dl.empty and "amount" in dl.columns:
            hs = dl[~dl["ts_code"].astype(str).str.endswith(".BJ")]   # 剔北交所·对齐幕数据沪深全推(否则量比偏低假缩量)
            tots.append(float(pd.to_numeric(hs["amount"], errors="coerce").sum()) * 1e3)  # 千元→元
    prev = tots[-1] if tots else 0.0
    avg5 = sum(tots) / len(tots) if tots else 0.0
    _BASE.update(date=today, prev=prev, avg5=avg5)
    return prev, avg5


def _curve_dir():
    d = get_settings().cache_dir / "market_vol_curve"
    d.mkdir(parents=True, exist_ok=True)
    return d


_LAST_REC = {"key": None}


def _record_curve(today: str, hm3: str, amount: float) -> None:
    """节流记录大盘分时Σamount(3分钟桶·每桶存最新值)·供同时段基线(Phase2)。"""
    key = (today, hm3)
    if _LAST_REC["key"] == key:
        return
    p = _curve_dir() / f"{today}.json"
    try:
        curve = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        curve[hm3] = amount
        p.write_text(json.dumps(curve), encoding="utf-8")
        _LAST_REC["key"] = key
    except Exception as e:
        logger.debug("[大盘量能] 曲线记录失败: %s", e)


_SAME = {"key": None, "val": None}


def _same_time_baseline(today: str, hm3: str, min_days: int = 3) -> float | None:
    """过去5日同一时刻(取≤hm3最近桶)大盘累计成交额均值·≥min_days天才用。缓存到3min桶。"""
    if _SAME["key"] == (today, hm3):
        return _SAME["val"]
    from app.nodes.quick_report import _recent_trade_dates
    ds = [d for d in _recent_trade_dates(_prov(), today, 8) if d < today][-5:]
    vals = []
    for d in ds:
        p = _curve_dir() / f"{d}.json"
        if not p.exists():
            continue
        try:
            curve = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        keys = sorted(k for k in curve if k <= hm3)
        if keys:
            vals.append(float(curve[keys[-1]]))
    val = (sum(vals) / len(vals)) if len(vals) >= min_days else None
    _SAME.update(key=(today, hm3), val=val)
    return val


def _label(vr: float | None) -> str:
    if vr is None:
        return "—"
    if vr >= 1.15:
        return "放量"
    if vr <= 0.85:
        return "缩量"
    return "平量"


def market_volume_block(df, session: str) -> dict | None:
    """大盘量能块。df=全推快照(含 amount·元)·休市时用收盘累计给全天口径。"""
    if df is None or df.empty or "amount" not in df.columns:
        return None
    today_amt = float(pd.to_numeric(df["amount"], errors="coerce").sum())
    if today_amt <= 0:
        return None
    now = datetime.datetime.now()
    today = now.strftime("%Y%m%d")
    prev, avg5 = _baseline(today)
    live = session == "continuous"
    hm3 = f"{now.hour:02d}{(now.minute // 3) * 3:02d}"
    if live:
        _record_curve(today, hm3, today_amt)                        # Phase2 分时曲线记录
    elapsed = _elapsed_trade_minutes(now)

    # 量比：同时段(精确·Phase2)优先 → 时段归一(近似) → 收盘后全天口径
    same = _same_time_baseline(today, hm3) if (live and elapsed) else None
    approx, vr = True, None
    if same and same > 0:
        vr, approx = today_amt / same, False                        # 精确·对齐东财
    elif live and elapsed and avg5 > 0:
        vr = (today_amt / elapsed) / (avg5 / _TRADE_MIN)            # 时段归一·近似
    elif not live and avg5 > 0:
        vr, approx = today_amt / avg5, False                        # 收盘后=全天口径·精确

    proj = today_amt * _TRADE_MIN / elapsed if (live and elapsed) else today_amt
    return {
        "today_yi": round(today_amt / 1e8),
        "prev_yi": round(prev / 1e8) if prev else None,
        "avg5_yi": round(avg5 / 1e8) if avg5 else None,
        "proj_yi": round(proj / 1e8),
        "proj_vs_prev": round((proj / prev - 1) * 100) if prev else None,
        "vol_ratio": round(vr, 2) if vr else None,
        "label": _label(vr),
        "approx": approx,
        "elapsed_min": elapsed,
        "live": live,
    }
