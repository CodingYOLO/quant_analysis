"""
共振确定性选股：给候选按【正交维度】共振打分（对标吴川「几个选股系统叠加=确定性高」）。

核心认知（回应用户"真的学会"）：
  确定性来自**独立维度的共振**；若各维度都在测同一件事(都是动量/都是热度)，叠加只是同质双重计数，是自欺。
  故本模块选 4 个尽量正交的维度：
    ① 板块强势(自上而下·方向) ② 龙虎榜真钱(资金·唯一真机构钱) ③ 入局区间到位(位置/砍价) ④ 基本面(价值)
  共振分 = 命中维度数(0-4)。再叠加**位置分级**（吴川"砍价·不追高"）：
    共振强 + 现价回踩到入局区间 = A(可低吸观察) ; 共振强但远离 = B(等回踩·别追)。

铁律：**不输出胜率、不排序买卖、只标共振与位置**。共振高 ≠ 无风险(可能拥挤)——位置分级即防追高。
纯函数打分 + 编排(取池/预取关键位/基本面)分离，便于单测与回测。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULTS = {"roe_min": 8.0, "grade_a_dims": 3}   # 阈值可调·待回测校准
_POS_RANK = {"in": 0, "watch": 1, "far": 2, "below": 3, "na": 4}


# ── 纯打分（可单测·可回测）────────────────────────────────────────────────────
def score_resonance(records: list[dict], levels_map: dict | None = None,
                    fin_map: dict | None = None, params: dict | None = None) -> list[dict]:
    """给候选记录打 4 维共振分 + 位置分级。levels_map/fin_map 由编排层预取注入(依赖注入)。"""
    p = {**DEFAULTS, **(params or {})}
    heats = sorted(r["theme_heat"] for r in records if r.get("theme_heat") is not None)
    heat_med = heats[len(heats) // 2] if heats else 0.0
    out = [c for r in records if (c := _score_one(r, levels_map, fin_map, heat_med, p))]
    out.sort(key=lambda x: (-x["resonance"], _POS_RANK.get(x["entry_state"], 4),
                            -(x["dims"]["realmoney"].get("val") or -9.0)))
    return out


def _score_one(r: dict, levels_map, fin_map, heat_med: float, p: dict) -> dict | None:
    code = r.get("ts_code")
    if not code:
        return None
    dims = {"sector": _dim_sector(r, heat_med), "realmoney": _dim_realmoney(r),
            "entry": _dim_entry(code, levels_map), "fundamental": _dim_fundamental(code, fin_map, p)}
    hits = sum(1 for d in dims.values() if d["hit"])
    est = dims["entry"].get("state", "na")
    return {"ts_code": code, "name": r.get("name"), "industry": r.get("industry"),
            "close": r.get("close"), "resonance": hits, "entry_state": est,
            "grade": _grade(hits, est, p), "dims": dims, "entry_zone": dims["entry"].get("zone")}


def _dim_sector(r: dict, heat_med: float) -> dict:
    """板块强势(自上而下)：所属板块热度 ≥ 池内中位 且 >0 → 该票在'对的方向'。相对阈值·抗量纲。"""
    h = r.get("theme_heat")
    hit = h is not None and h > 0 and h >= heat_med
    return {"hit": bool(hit), "val": h,
            "label": f"{r.get('industry', '板块')} 热度{h}" + ("·偏强" if hit else "·一般")}


def _dim_realmoney(r: dict) -> dict:
    """龙虎榜机构真钱：近5日机构净买 > 0（A股仅有的个股级真机构钱·非估算）。"""
    net, days = r.get("inst_net_yi"), r.get("inst_buy_days") or 0
    hit = net is not None and net > 0
    label = f"龙虎榜机构净买 {net:+.2f}亿·{days}天(真钱)" if hit else "近5日无机构真钱印证"
    return {"hit": bool(hit), "val": net, "days": days, "label": label}


def _dim_entry(code: str, levels_map) -> dict:
    """入局区间到位(砍价位置)：现价回踩到入局区间/警戒带(关键位 state in/watch)。"""
    lv = (levels_map or {}).get(code)
    if not lv or not lv.get("position"):
        return {"hit": False, "state": "na", "label": "无关键位数据", "zone": None}
    pos = lv["position"]
    st = pos.get("state", "na")
    return {"hit": st in ("in", "watch"), "state": st,
            "label": pos.get("label", ""), "zone": lv.get("entry_zone")}


def _dim_fundamental(code: str, fin_map, p: dict) -> dict:
    """基本面(价值·独立于价格)：ROE 达标 且 净利同比为正。防纯情绪票。"""
    f = (fin_map or {}).get(code)
    if not f:
        return {"hit": False, "label": "无基本面数据"}
    roe, yoy = f.get("roe"), f.get("yoy")
    hit = (roe is not None and roe >= p["roe_min"]) and (yoy is not None and yoy > 0)
    parts = []
    if roe is not None:
        parts.append(f"ROE{roe:.1f}%")
    if yoy is not None:
        parts.append(f"净利同比{yoy:+.0f}%")
    return {"hit": bool(hit), "roe": roe, "yoy": yoy, "label": "·".join(parts) or "—"}


def _grade(hits: int, est: str, p: dict) -> str:
    """A=多维共振+砍价到位 · B=共振强但要等回踩(别追) · C=2维 · D=弱。"""
    if hits >= p["grade_a_dims"] and est in ("in", "watch"):
        return "A"
    if hits >= p["grade_a_dims"]:
        return "B"
    return "C" if hits == 2 else "D"


# ── 编排（有 IO）：取池 → 预取关键位/基本面 → 打分 ───────────────────────────
def run_resonance(provider, trade_date: str | None = None,
                  market_label: str = "震荡", params: dict | None = None) -> dict:
    """完整流程：选股池为候选池 → 补入局区间+基本面 → 4维共振打分 + 位置分级。"""
    import datetime

    from app.factors.breadth_qfq import _recent_trade_dates
    from app.strategy.stock_pool import build_stock_pool
    td = trade_date or _recent_trade_dates(provider, datetime.date.today().strftime("%Y%m%d"), 1)[-1]
    records = build_stock_pool(td, provider, market_label=market_label, persist=False) or []
    if not records:
        return {"ok": True, "trade_date": td, "candidates": [], "n_pool": 0,
                "note": "选股池为空(可能弱市/非交易日)"}
    levels_map = _levels_for(provider, [r["ts_code"] for r in records])
    fin_map = _fin_for(provider, records)
    scored = score_resonance(records, levels_map, fin_map, params)
    counts = {g: sum(1 for x in scored if x["grade"] == g) for g in ("A", "B", "C", "D")}
    return {"ok": True, "trade_date": td, "n_pool": len(records),
            "candidates": scored, "grade_counts": counts}


def _levels_for(provider, ts_codes: list[str]) -> dict:
    """预取每票关键位(复用 tech_chain._zone_for·按交易日缓存)。键=完整 ts_code。"""
    from app.strategy.tech_chain import _zone_for
    out = {}
    for ts in ts_codes:
        try:
            out[ts] = _zone_for(provider, str(ts).split(".")[0])
        except Exception:
            out[ts] = None
    return out


def _fin_for(provider, records: list[dict]) -> dict:
    """批量取基本面(最近已披露季度·全市场一把)→ {ts_code: {roe, yoy}}。"""
    import pandas as pd
    fm: dict = {}
    for period in _recent_periods():
        try:
            df = provider.get_fina_indicator_by_period(period)
        except Exception:
            df = None
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                ts = row.get("ts_code")
                if ts and ts not in fm:
                    fm[ts] = {"roe": _f(pd.to_numeric(row.get("roe"), errors="coerce")),
                              "yoy": _f(pd.to_numeric(row.get("netprofit_yoy"), errors="coerce"))}
            break
    return fm


def _recent_periods() -> list[str]:
    """最近若干季度末(倒序·≤今日)。"""
    import datetime
    y = datetime.date.today().year
    today = datetime.date.today().strftime("%Y%m%d")
    qs = [f"{yr}{md}" for yr in (y, y - 1) for md in ("1231", "0930", "0630", "0331")]
    return sorted([q for q in qs if q <= today], reverse=True)[:4]


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None
