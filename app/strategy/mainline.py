"""
今日主线 + 龙头：把扁平资金榜升级为"该盯哪条线、龙头是谁、为什么"的客观综合判断。

口径（用户确认·涨停梯队为主）：主线强度 = 涨停梯队 45% + 资金 35% + 涨幅 20%
  - 涨停梯队（灵魂·绝对分）：概念内涨停家数 + 最高连板高度（连板越高越加权）。无涨停≠主线。
  - 资金（真金确认·候选内百分位）：概念今日主力净流入（同花顺口径·剔宽概念）。
  - 涨幅（强度佐证·候选内百分位）：概念今日涨幅。
龙头 = 概念内最高连板个股（同板由今日主力净流入定高下）。涨停原因取开盘啦官方（非臆测）。

诚实纪律：主线/龙头均为**当下客观综合描述**（此刻哪条线最强），**非预测明日、非荐股**。
数据：涨停/连板 limit_list_d、涨停原因 开盘啦 kpl_list、概念资金 同花顺 moneyflow_cnt_ths。
"""

from __future__ import annotations

import logging
from collections import Counter

from app.data.composite_provider import CompositeProvider
from app.data.moneyflow import main_net_wan

logger = logging.getLogger(__name__)

_W_LADDER, _W_FUND, _W_RET = 0.45, 0.35, 0.20   # 权重（用户确认：涨停梯队为主）
_ZT_PER_STOCK, _ZT_PER_LB = 8.0, 22.0           # 涨停梯队绝对分：每家 + 每级连板高度
_DEFAULT_TOP = 15
_DEDUPE_OVERLAP = 0.6                            # 涨停股包含度≥此值→视为同题材·并入更强的主线（透明记录）


def build_mainline(date: str, provider: CompositeProvider | None = None, top: int = _DEFAULT_TOP) -> dict:
    """
    构建今日主线榜（指定交易日）。

    Returns:
        {date, lines: [主线卡片...], zt_total, note}。当日无涨停时 lines 为空并给出弱势说明。
    """
    prov = provider or CompositeProvider()
    zt = _load_zt(date, prov)
    if not zt:
        return {"date": date, "lines": [], "zt_total": 0,
                "note": "当日无涨停股·弱势格局——今日无明确主线（涨停梯队为主的口径下）。"}

    zt_by_code = {r["code"]: r for r in zt}
    net = _stock_net_map(date, prov)              # {ts_code: 今日主力净流入(亿)}
    flow_by_name = _concept_flow_map(date, prov)  # {概念: 资金/涨幅/宽概念标}
    members = _concept_members(prov)              # {概念: [成分ts_code]}

    lines = []
    for concept, codes in members.items():
        card = _build_line(concept, codes, zt_by_code, flow_by_name, net)
        if card is not None:
            lines.append(card)
    _score_lines(lines)
    lines.sort(key=lambda c: c["score"], reverse=True)
    lines = _dedupe_lines(lines)                   # 合并高度重叠的同题材概念（透明·记录被并入项）
    for c in lines:
        c.pop("_zt_codes", None)
    return {
        "date": date, "zt_total": len(zt), "lines": lines[:top],
        "note": ("主线强度=涨停梯队45%+资金35%+涨幅20%（涨停梯队为主）。资金=同花顺概念主力净流入(估算·非龙虎榜真钱)、"
                 "涨停原因=开盘啦官方。**客观综合描述·非预测/非荐股**；龙头=当日最高连板，不是买入建议。"),
    }


def _build_line(concept: str, codes: list, zt_by_code: dict, flow_by_name: dict, net: dict) -> dict | None:
    """单概念 → 主线卡片。剔宽概念/无资金数据/无涨停（无涨停≠主线）。"""
    frow = flow_by_name.get(concept)
    if frow is None or frow.get("broad"):
        return None
    zt_in = [zt_by_code[c] for c in codes if c in zt_by_code]
    if not zt_in:
        return None
    lead = max(zt_in, key=lambda r: (r["limit_times"], net.get(r["code"], 0.0)))
    return {
        "concept": concept,
        "zt_count": len(zt_in),
        "max_lb": max(r["limit_times"] for r in zt_in),
        "ladder": _ladder_summary(zt_in),
        "lead": {
            "code": lead["code"], "name": lead["name"], "lb": lead["limit_times"],
            "open_times": lead["open_times"], "amount_yi": lead["amount_yi"],
            "net": round(net.get(lead["code"], 0.0), 2),
            "reason": lead.get("reason") or lead.get("theme") or "",
        },
        "today_net": frow.get("today_net"),
        "cum3": frow.get("cum3"),
        "today_pct": frow.get("today_pct"),
        "_zt_codes": {r["code"] for r in zt_in},
    }


def _dedupe_lines(lines: list) -> list:
    """按分数降序贪心去重：涨停股高度包含于已保留主线的概念 → 并入（记 merged·非静默丢弃）。"""
    kept: list = []
    for c in lines:
        s = c["_zt_codes"]
        host = next((k for k in kept if _containment(s, k["_zt_codes"]) >= _DEDUPE_OVERLAP), None)
        if host is None:
            c["merged"] = []
            kept.append(c)
        else:
            host["merged"].append(c["concept"])
    return kept


def _containment(a: set, b: set) -> float:
    """两涨停股集合的包含度 = 交集 / 较小集合大小（小概念被大概念覆盖时→1）。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _ladder_summary(zt_in: list) -> str:
    """连板梯队摘要：'3板×1 / 2板×2 / 首板×5'（高板在前）。"""
    c = Counter(min(r["limit_times"], 9) for r in zt_in)
    return " / ".join(f"{'首板' if lb == 1 else f'{lb}板'}×{c[lb]}" for lb in sorted(c, reverse=True))


def _score_lines(lines: list) -> None:
    """写入每条主线的 score（0-100）：涨停梯队绝对分 + 资金/涨幅候选内百分位·加权。"""
    if not lines:
        return
    _assign_pct(lines, "today_net", "_m_pct")
    _assign_pct(lines, "today_pct", "_r_pct")
    for c in lines:
        ladder = min(100.0, c["zt_count"] * _ZT_PER_STOCK + (c["max_lb"] - 1) * _ZT_PER_LB)
        c["t_score"] = round(ladder, 1)
        c["score"] = round(_W_LADDER * ladder + _W_FUND * c["_m_pct"] + _W_RET * c["_r_pct"], 1)
        c.pop("_m_pct", None)
        c.pop("_r_pct", None)


def _assign_pct(lines: list, key: str, out_key: str) -> None:
    """按 key 值给各候选打百分位分（0-100·按秩·None 视作最小）。"""
    order = sorted(range(len(lines)), key=lambda i: (lines[i].get(key) if lines[i].get(key) is not None else -1e18))
    n = len(lines)
    for rank, i in enumerate(order):
        lines[i][out_key] = round(rank / (n - 1) * 100, 1) if n > 1 else 100.0


# ── 数据装配（复用现有已验证模块·不重造）─────────────────────────────────────
def _load_zt(date: str, prov: CompositeProvider) -> list:
    """当日涨停股记录（含连板/封板/开板/开盘啦题材原因）——复用 limitup_review._zt_records。"""
    from app.strategy.limitup_review import _zt_records
    return _zt_records(date, prov)


def _stock_net_map(date: str, prov: CompositeProvider) -> dict:
    """{ts_code: 今日主力净流入(亿)}——elg+lg 东财口径（canonical）。"""
    try:
        mf = prov.get_money_flow(date)
        s = main_net_wan(mf) / 1e4   # 万元 → 亿
        return {str(k): float(v) for k, v in s.items() if v == v}   # 剔 NaN
    except Exception as e:
        logger.warning("[主线] 个股主力净流入获取失败: %s", e)
        return {}


def _concept_flow_map(date: str, prov: CompositeProvider) -> dict:
    """{概念: {today_net, cum3, today_pct, broad}}——复用概念持续流入榜（近5日窗·含宽概念标）。"""
    from app.strategy.concept_flow import build_concept_persistent_flow
    try:
        rows = build_concept_persistent_flow(date, window=5, provider=prov)["rows"]
        return {r["concept"]: r for r in rows}
    except Exception as e:
        logger.warning("[主线] 概念资金获取失败: %s", e)
        return {}


def _concept_members(prov: CompositeProvider) -> dict:
    """{概念: [成分ts_code]}——优先宽成分 map（覆盖大概念）·回退窄 map。"""
    from app.factors.theme_wide import concept_members_map
    from app.strategy.concept_flow import _concept_member_codes_wide
    return _concept_member_codes_wide(prov) or concept_members_map(prov)
