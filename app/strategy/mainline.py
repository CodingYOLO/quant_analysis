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
        c["outlook"] = _continuity_verdict(c)      # 明日延续性研判（客观信号·非预测）
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
        "consec": frow.get("consec_days"),      # 连续净流入天(资金持续性)
        "flow_delta": frow.get("delta1d"),      # 今日资金较昨变化(加速/减速)
        "ambush": bool(frow.get("ambush")),     # 资金进但价没涨(埋伏蓄势)
        "n_tiers": len({min(r["limit_times"], 9) for r in zt_in}),  # 连板梯队级数(厚度)
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


# ── 明日延续性研判（客观信号·非预测/非胜率）─────────────────────────────────────
# 游资情绪周期视角：资金持续+梯队健康+龙头封死+埋伏=延续特征；断层/透支/烂板=退潮特征。
# 只描述"结构上更可能延续/退潮"·绝不输出胜率/必涨/买卖建议。
def _continuity_verdict(c: dict) -> dict:
    """单主线明日延续性研判 → {level, label, score, pos[], neg[]}。纯客观信号加减分。"""
    pos: list = []
    neg: list = []
    score = 0
    consec = c.get("consec") or 0
    lead_open = c["lead"].get("open_times") or 0
    if consec >= 3:
        score += 2; pos.append(f"资金连续{consec}日净流入")
    if (c.get("flow_delta") or 0) > 0:
        score += 1; pos.append("今日资金较昨加速")
    if c["max_lb"] >= 3 and c.get("n_tiers", 1) >= 3:
        score += 2; pos.append(f"梯队健康(最高{c['max_lb']}板·{c['n_tiers']}级接力)")
    if lead_open == 0:
        score += 1; pos.append("龙头封死未开板")
    if c.get("ambush"):
        score += 1; pos.append("资金埋伏(进而涨幅未透支)")
    if c["max_lb"] >= 4 and c["zt_count"] <= 2:
        score -= 2; neg.append(f"一枝独秀(最高{c['max_lb']}板但仅{c['zt_count']}家·梯队断层)")
    if (c.get("today_net") or 0) < 0 and (c.get("today_pct") or 0) >= 2:
        score -= 2; neg.append("涨幅透支(概念大涨但主力净流出)")
    if lead_open >= 3:
        score -= 1; neg.append(f"龙头烂板(开板{lead_open}次)")
    level = "strong" if score >= 4 else ("fade" if score <= 0 else "mixed")
    label = {"strong": "强延续", "fade": "退潮风险", "mixed": "分歧待选手"}[level]
    return {"level": level, "label": label, "score": score, "pos": pos, "neg": neg}


# ── AI 明日展望（LLM 定性·接地财联社+博查·严禁编造·按日缓存）─────────────────────
def build_ai_outlook(date: str, provider: CompositeProvider | None = None, force: bool = False) -> dict:
    """今日主线 + LLM 明日展望（延续/退潮定性研判·带消息催化·非预测非荐股）。按日缓存·force 重算(盘后预热用)。"""
    from app.strategy import detail_common as DC
    path = DC.cache_path("mainline_outlook", date, "ai")
    if not force:
        cached = DC.load_cache(path)
        if cached is not None:
            return cached
    prov = provider or CompositeProvider()
    main = build_mainline(date, prov, top=10)
    lines = main["lines"]
    if not lines:
        data = {"date": date, "outlook": "今日无涨停·弱势格局——明日无明确主线可延续。", "lines": [], "cached": False}
        DC.save_cache(path, data)
        return data
    headlines = DC.macro_headlines(prov, date)
    keys = {c["concept"] for c in lines} | {c["lead"]["name"] for c in lines}
    news = DC.relevant_news(headlines, keys)
    data = {"date": date, "outlook": _llm_outlook(_outlook_context(lines), news),
            "lines": lines, "cached": False}
    DC.save_cache(path, data)
    return data


def _outlook_context(lines: list) -> str:
    """把今日主线 + 延续信号压成 LLM 可读的结构化摘要。"""
    rows = []
    for i, c in enumerate(lines, 1):
        o = c["outlook"]
        sig = "；".join(o["pos"] + [f"⚠{x}" for x in o["neg"]]) or "无显著信号"
        rows.append(f"{i}. {c['concept']}｜{o['label']}｜涨停{c['zt_count']}家最高{c['max_lb']}板"
                    f"｜龙头{c['lead']['name']}({c['lead']['lb']}板,主力{c['lead']['net']:+.1f}亿)"
                    f"｜今净流入{c.get('today_net')}亿｜信号:{sig}")
    return "\n".join(rows)


def _llm_outlook(context: str, news: list) -> str:
    """调 LLM 出明日展望（严诚实框·禁胜率/荐股/编造）。失败回退纯客观提示。"""
    from app.llm.client import LLMClient
    news_txt = "\n".join(f"- {n}" for n in news[:12]) or "（今日无精筛到的相关消息）"
    prompt = (
        "你是A股短线情绪周期分析师。基于下方【今日主线+延续性客观信号】与【今日相关消息】，给出【明日展望】。\n"
        "硬性要求：①只做延续性研判(哪些线结构上更可能延续、哪些更可能退潮/分歧，并说明依据情绪周期：启动/主升/分歧/退潮)；"
        "②严禁输出胜率/成功率/『必涨』等确定性措辞，严禁荐股与买卖建议，严禁编造任何数据或消息(只能用给到的)；"
        "③结构：〖明日或延续〗1-3条线+逻辑 / 〖警惕退潮或分歧〗+信号 / 〖关键催化看点〗(基于给定消息)；"
        "④结尾一句风险提示。150-260字，中文，务实不废话。\n\n"
        f"【今日主线+延续信号】\n{context}\n\n【今日相关消息】\n{news_txt}"
    )
    try:
        return LLMClient().chat([{"role": "user", "content": prompt}], task_type="pro").strip()
    except Exception as e:
        logger.warning("[主线] LLM 明日展望失败: %s", e)
        return "（AI 明日展望暂不可用，请参考各主线的客观延续性研判。）"
