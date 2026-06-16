"""
M5：板块全景看板分类引擎（纯因子，无 LLM）。

读中枢宽表 theme_heat_all_in_one，按规则把板块打到三类互不排斥的诊断视角：
  - 轮动上行：资金流入 + 强度共振
  - 低吸观察：回撤但结构未破
  - 高位风险：涨幅高 + 拥挤度高

同一板块可同时命中多类（互不排斥）。所有阈值集中在 _THRESHOLDS，便于对照原站校准。
"""

from __future__ import annotations

import logging

from app.data.theme_heat_db import get_themes, latest_trade_date

logger = logging.getLogger(__name__)

# 分类阈值（配置化，便于对照吴川原站回归校准）
_THRESHOLDS = {
    "rotate_breadth_ma20": 50.0,    # 轮动上行：MA20 广度下限
    "dip_breadth_ma20": 40.0,       # 低吸观察：MA20 广度下限（结构未破）
    "risk_pct_7d": 10.0,            # 高位风险：7 日涨幅下限
    "risk_top100": 12.0,            # 高位风险：Top100 拥挤度下限(%)（行业/概念通用）
}


def _is_rotate(r: dict) -> bool:
    """轮动上行：3日资金>0 且 3日涨>0 且 MA20广度≥阈值。"""
    return (
        _num(r.get("money_flow_3d")) > 0
        and _num(r.get("pct_chg_3d")) > 0
        and _num(r.get("breadth_ma20")) >= _THRESHOLDS["rotate_breadth_ma20"]
    )


def _is_dip(r: dict) -> bool:
    """低吸观察：当日回调(1日跌) 但 资金仍净流入(3日>0) 且 MA20广度未破阈值。"""
    return (
        _num(r.get("pct_chg_1d")) < 0
        and _num(r.get("money_flow_3d")) > 0
        and _num(r.get("breadth_ma20")) >= _THRESHOLDS["dip_breadth_ma20"]
    )


def _is_risk(r: dict) -> bool:
    """高位风险：7日涨幅高 且 Top100 拥挤度高（行业/概念通用口径）。"""
    return (
        _num(r.get("pct_chg_7d")) >= _THRESHOLDS["risk_pct_7d"]
        and _num(r.get("top100_ratio")) >= _THRESHOLDS["risk_top100"]
    )


def _num(v) -> float:
    """None/非数 → -inf（不命中任何 ≥ 阈值判断），用于安全比较。"""
    try:
        return float(v) if v is not None else float("-inf")
    except (TypeError, ValueError):
        return float("-inf")


def build_sectorscope(date: str = "", theme_types: tuple[str, ...] = ("industry", "concept")) -> dict:
    """
    构建板块全景数据。

    Args:
        date: 交易日 YYYYMMDD；空则取宽表最近已计算日。
        theme_types: 纳入的板块类型。

    Returns:
        {ok, available, date, rows, buckets:{rotate,dip,risk}}。rows 含 signal 标签。
        无数据时 available=False（不展示旧/假数据）。
    """
    d = (date or "").replace("-", "") or (latest_trade_date() or "")
    if not d:
        return {"available": False, "date": "", "rows": [], "buckets": {},
                "msg": "宽表尚未计算，请先运行 python -m app.run wide"}

    rows: list[dict] = []
    for t in theme_types:
        rows.extend(get_themes(d, t))
    if not rows:
        return {"available": False, "date": d, "rows": [], "buckets": {},
                "msg": f"{d} 宽表未计算（数据缺失，不展示旧/假数据）"}

    rotate, dip, risk = [], [], []
    for r in rows:
        flags = []
        if _is_rotate(r):
            flags.append("轮动")
            rotate.append(r)
        if _is_dip(r):
            flags.append("低吸")
            dip.append(r)
        if _is_risk(r):
            flags.append("高位风险")
            risk.append(r)
        r["signal"] = flags[0] if flags else ""
        r["signals"] = flags

    # 各栏按相关强度排序
    rotate.sort(key=lambda r: _num(r.get("money_flow_3d")), reverse=True)
    dip.sort(key=lambda r: _num(r.get("money_flow_3d")), reverse=True)
    risk.sort(key=lambda r: _num(r.get("pct_chg_7d")), reverse=True)

    return {
        "available": True,
        "date": d,
        "rows": rows,
        "buckets": {
            "rotate": rotate[:12],
            "dip": dip[:12],
            "risk": risk[:12],
        },
    }
