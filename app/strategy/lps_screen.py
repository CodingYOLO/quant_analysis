"""LPS「突破回踩」选股：全市场扫 wyckoff.lps_entry（放量突破后缩量回踩不破·威科夫最可靠买点）。

回测验证后落地（evaluate_vpa_pre_markup·2026-06跌市/2026-03上行两期均跑赢基准·抗跌+进攻）。
输出：排序榜 + 突破位/箱顶关键位 + 距突破位% + 距SOS天数 + 阶段标签。客观结构·非买卖建议·非荐股。

排序质量 = 回踩到位（现价越贴突破位越是买点）+ 新鲜（距SOS越近越好）。阈值走 wyckoff._LPS（回测过·可网格校准）。
"""

from __future__ import annotations

import datetime
import logging
import time

import pandas as pd

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

_cache: dict = {"date": "", "ts": 0.0, "data": None}
_TTL = 1800.0                     # 30min（全市场扫重·日内变化小·盘中30min刷新足够）


def build_lps_screen(date: str = "", provider: CompositeProvider | None = None,
                     min_circ_yi: float = 30.0, top: int = 80) -> dict:
    """全市场 LPS 榜。{ok, date, n, rows:[{code,name,industry,price,level,box_top,dist,days_since_sos,quality}]}。"""
    provider = provider or CompositeProvider()
    end = (date or "").replace("-", "") or _latest_trade_date(provider)
    now = time.time()
    if _cache["data"] and _cache["date"] == end and now - _cache["ts"] < _TTL:
        return _cache["data"]

    from app.backtest.wyckoff_eval import _liquid_universe, _name_map
    from app.data.history_loader import load_price_matrix
    from app.factors.wyckoff import lps_entry

    close_m, _open_m, high_m, low_m, vol_m = load_price_matrix(end, provider, n_days=150)
    cols = set(close_m.columns)
    universe = _liquid_universe(provider, end, min_circ_yi, cols)
    names = _name_map(provider)
    inds = _industry_map(provider)

    rows = []
    for code in universe:
        nm = str(names.get(code, "") or "")
        if "ST" in nm or "退" in nm:                          # 剔 ST/退市
            continue
        s = pd.to_numeric(close_m[code], errors="coerce").dropna()
        if len(s) < 90:
            continue
        hi = pd.to_numeric(high_m[code], errors="coerce").reindex(s.index)
        lo = pd.to_numeric(low_m[code], errors="coerce").reindex(s.index)
        v = pd.to_numeric(vol_m[code], errors="coerce").reindex(s.index)
        r = lps_entry(s, hi, lo, v)
        if not r.get("is_lps"):
            continue
        cur = float(s.iloc[-1])
        level = r["level"]
        dist = round((cur / level - 1) * 100, 1) if level else None      # 现价距突破位%（越小越贴买点）
        days = r.get("days_since_sos") or 16
        # 质量 = 回踩到位（现价贴突破位）+ 新鲜（距SOS近）。形态诊断(箱幅/冲高)不入排序——
        # 回测证实收严只留教科书横盘会抹掉动量 edge，故只作展示供自筛，不做打分权重。
        quality = round(max(0.0, 20 - abs(dist if dist is not None else 20)) + max(0.0, 16 - days), 1)
        rows.append({
            "code": code[:6], "name": nm, "industry": inds.get(code, ""),
            "price": round(cur, 2), "level": level, "box_lo": r.get("box_lo"),
            "box_range": r.get("box_range"),        # 箱幅%（前区间高低差·小=真横盘·大=已上行）
            "base_trend": r.get("base_trend"),      # 箱体净上行%（大=伪箱体/已主升浪）
            "excursion": r.get("excursion"),        # 突破后冲高%（大=追高·主升浪已走）
            "dist": dist, "days_since_sos": days, "quality": quality,
        })
    rows.sort(key=lambda x: -x["quality"])
    data = {"ok": True, "date": end, "n": len(rows), "rows": rows[:top]}
    _cache.update(date=end, ts=now, data=data)
    return data


def _latest_trade_date(provider: CompositeProvider) -> str:
    """最近一个已结算交易日（有 daily_basic 的）。"""
    from app.nodes.quick_report import _recent_trade_dates
    today = datetime.date.today().strftime("%Y%m%d")
    for d in reversed(_recent_trade_dates(provider, today, 4) or [today]):
        try:
            db = provider.get_daily_basic(d)
            if db is not None and not db.empty:
                return d
        except Exception:
            continue
    return today


def _industry_map(provider: CompositeProvider) -> dict:
    """{ts_code: 申万二级行业}。"""
    try:
        sb = provider.get_stock_basic()
        if sb is None or sb.empty or "industry" not in sb.columns:
            return {}
        return dict(zip(sb["ts_code"], sb["industry"].fillna("")))
    except Exception as e:
        logger.warning("[LPS选股] 行业映射失败: %s", e)
        return {}
