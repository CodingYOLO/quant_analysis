"""自选作战地图：把自选/持仓按【所属申万二级行业】分组，叠加板块阶段（进攻/防守/过热），
并汇总一份「明日重点」清单——降低"看了半天不下手"的决策阻力。

设计原则（治拖延）：
  1. 板块阶段提前判好（复用 sector_scope 的 轮动=进攻 / 低吸=防守 / 高位=过热）；
  2. 个股关键位复用 watch_levels（现价距支撑/压力%、MA站上/跌破、量比换手）；
  3. 明日重点 = 客观现状 + 机械触发条件（**不出现买/卖/该减等方向性措辞**）。

纯组装现有件（watch_levels + sector_scope + 申万行业映射），不新增数据源。
"""

from __future__ import annotations

import logging

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

# 板块阶段标签（图标+文字·不靠红绿语义避免歧义）
PHASE_ATTACK = {"key": "attack", "label": "⚡进攻", "desc": "轮动上行·资金流入+结构健康"}
PHASE_DEFEND = {"key": "defend", "label": "🛡防守", "desc": "低吸观察·回撤未破位"}
PHASE_HOT = {"key": "hot", "label": "🔥过热", "desc": "高位风险·涨幅居前+拥挤"}
PHASE_NEUTRAL = {"key": "neutral", "label": "中性", "desc": "无明确阶段信号"}

_NEAR_SUPPORT = 3.0     # 距支撑 ≤3% 视为"接近支撑"
_NEAR_RESIST = 2.0      # 距压力 ≤2% 视为"接近压力"


def build_watch_battlemap(provider: CompositeProvider | None = None) -> dict:
    """自选作战地图：{ok, date, live, groups:[...], shortlist:[...]}。"""
    provider = provider or CompositeProvider()
    from app.strategy.watch_levels import build_watch_levels
    wl = build_watch_levels(provider=provider)
    rows = wl.get("rows") or []
    if not rows:
        return {"ok": True, "date": wl.get("date", ""), "live": wl.get("live", False),
                "groups": [], "shortlist": []}

    code2ind = _industry_map(provider, rows)
    phase_of, sector_of = _sector_context()
    groups = _group_by_sector(rows, code2ind, phase_of, sector_of)
    shortlist = _build_shortlist(groups)
    return {"ok": True, "date": wl.get("date", ""), "live": wl.get("live", False),
            "groups": groups, "shortlist": shortlist}


# ── 票 → 申万二级行业 ─────────────────────────────────────────────────────────
def _industry_map(provider: CompositeProvider, rows: list[dict]) -> dict:
    """{6位代码: 申万二级行业名}。get_stock_basic().industry 已是申万二级口径。"""
    try:
        sb = provider.get_stock_basic()
        if sb is None or sb.empty or "industry" not in sb.columns:
            return {}
        return {str(r["ts_code"])[:6]: (r.get("industry") or "其他")
                for _, r in sb.iterrows()}
    except Exception as e:
        logger.warning("[作战地图] 行业映射失败: %s", e)
        return {}


# ── 板块阶段 + 板块因子（复用 sector_scope 宽表口径）──────────────────────────
def _sector_context() -> tuple[dict, dict]:
    """返回 (phase_of{行业名: PHASE_*}, sector_of{行业名: 板块因子行})。"""
    try:
        from app.strategy.sector_scope import build_sectorscope
        sc = build_sectorscope(theme_types=("industry",))
    except Exception as e:
        logger.warning("[作战地图] 板块全景取用失败: %s", e)
        return {}, {}
    if not sc.get("available"):
        return {}, {}
    buckets = sc.get("buckets") or {}
    names = lambda b: {r.get("theme_name") for r in (buckets.get(b) or [])}
    attack, defend, hot = names("rotate"), names("dip"), names("risk")
    phase_of: dict = {}
    for nm in hot:
        phase_of[nm] = PHASE_HOT           # 过热优先级最高（先避险）
    for nm in attack:
        phase_of.setdefault(nm, PHASE_ATTACK)
    for nm in defend:
        phase_of.setdefault(nm, PHASE_DEFEND)
    sector_of = {r.get("theme_name"): r for r in (sc.get("rows") or [])}
    return phase_of, sector_of


def _group_by_sector(rows: list[dict], code2ind: dict,
                     phase_of: dict, sector_of: dict) -> list[dict]:
    """按行业分组；组内持仓在前。组按 过热→进攻→防守→中性、再按板块热度排序。"""
    by_ind: dict = {}
    for r in rows:
        ind = code2ind.get(r["code"], "其他")
        by_ind.setdefault(ind, []).append(r)

    groups = []
    for ind, stocks in by_ind.items():
        phase = phase_of.get(ind, PHASE_NEUTRAL)
        sd = sector_of.get(ind) or {}
        stocks.sort(key=lambda s: (not s["is_holding"], s["name"]))
        groups.append({
            "sector": ind,
            "phase": phase["key"], "phase_label": phase["label"], "phase_desc": phase["desc"],
            "sector_pct_1d": _num(sd.get("pct_chg_1d")),
            "sector_mf_1d": _num(sd.get("money_flow_1d")),
            "sector_mf_3d": _num(sd.get("money_flow_3d")),
            "sector_breadth_ma20": _num(sd.get("breadth_ma20")),
            "sector_heat": _num(sd.get("heat_score")),
            "stocks": stocks,
        })
    order = {"hot": 0, "attack": 1, "defend": 2, "neutral": 3}
    groups.sort(key=lambda g: (order.get(g["phase"], 9), -(g["sector_heat"] or 0)))
    return groups


# ── 明日重点 shortlist（描述现状 + 触发条件·无方向性措辞）────────────────────
def _build_shortlist(groups: list[dict]) -> list[dict]:
    """从分组结果派生：低吸观察 / 上冲留意 / 破位防守 三类，各带客观触发条件。"""
    out: list[dict] = []
    for g in groups:
        for s in g["stocks"]:
            item = _classify_stock(s, g)
            if item:
                out.append(item)
    rank = {"watch": 0, "caution": 1, "defend": 2}
    out.sort(key=lambda x: (rank.get(x["kind"], 9), x.get("_sort", 0)))
    for x in out:
        x.pop("_sort", None)
    return out


def _classify_stock(s: dict, g: dict) -> dict | None:
    """单票 → 明日重点条目（或 None）。仅客观陈述 + 触发条件，不给买卖指令。"""
    price, ma20 = _num(s.get("price")), _num(s.get("ma20"))
    sup, res = s.get("support"), s.get("resistance")
    below_ma20 = price is not None and ma20 is not None and price < ma20
    phase = g["phase"]
    name, sector = s["name"], g["sector"]

    def base(kind, tag, reason):
        return {"code": s["code"], "name": name, "sector": sector,
                "phase": phase, "phase_label": g["phase_label"],
                "kind": kind, "tag": tag, "reason": reason}

    # 🚨 破位防守：跌破 MA20 且板块过热/无支撑 → 客观"破位状态"
    if below_ma20 and phase in ("hot", "neutral"):
        it = base("defend", "🚨 破位",
                  f"现价已跌破 MA20（{ma20}）· 板块{g['phase_label']} · 属破位状态，反弹不过 MA20 为弱")
        it["_sort"] = 0
        return it

    # 🎯 低吸观察：接近支撑 + 未破 MA20 + 板块非过热
    if sup and sup.get("dist") is not None and abs(sup["dist"]) <= _NEAR_SUPPORT \
            and not below_ma20 and phase in ("attack", "defend", "neutral"):
        mf = g.get("sector_mf_1d")
        mftxt = "板块资金流入" if (mf or 0) > 0 else "板块资金分歧"
        it = base("watch", "🎯 低吸观察",
                  f"现价距支撑 {sup['dist']}%（{sup['price']}）· {mftxt}·{g['phase_label']} · 回踩不破可留意")
        it["_sort"] = abs(sup["dist"])
        return it

    # ⚠️ 上冲留意：接近压力 + 板块过热
    if res and res.get("dist") is not None and res["dist"] <= _NEAR_RESIST \
            and phase == "hot":
        it = base("caution", "⚠️ 上冲留意",
                  f"现价距压力 {res['dist']}%（{res['price']}）· 板块高位拥挤 · 上冲遇阻需留意")
        it["_sort"] = res["dist"]
        return it
    return None


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return round(f, 2) if f == f else None
    except (TypeError, ValueError):
        return None
