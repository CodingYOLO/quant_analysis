"""
市场「主线板块」研判：资金面 + 催化剂 + 政策 的接地式 LLM 综合推演。

用途：板块资金页 / 概念资金页 顶部的「今日主线研判」卡片——每日综合：
  1. 资金面（真实数据·已算）：行业资金持续流入榜 + 概念渗透率榜(相对强度) + 暗流(资金进价没涨=埋伏)
  2. 消息/政策面（真实来源）：财联社电报精筛 + 博查联网检索（含 URL/日期·可核查）
  3. LLM(pro) 综合：挑出「资金 + 催化剂/政策」共振的主线候选，每个带 资金证据 / 催化剂(来源) / 风险证伪点

纪律（严格遵守）：
  - **严禁编造**：LLM 只能用下方真实信息源，无催化剂就如实写「纯资金面·缺题材验证」（对齐 ai-data-must-be-sourced）。
  - **非投资建议**：可给方向性研究判断，但不用「买入/抄底/追高/满仓」下单措辞、不打包票必涨；
    每个候选必须给「风险/证伪点」，最终由用户判断（对齐 no-directional-recommendations + ANALYST_STANCE）。
  - **来源可核查**：财联社新闻 + 博查网页原样返回给前端展示（用户可点开核对）。

成本：整份按交易日缓存（含 LLM 结果），19:25 暖机预算一次；task_type=pro（低频高价值·夜间预算·无人等待）。
"""

from __future__ import annotations

import logging

from app.data.composite_provider import CompositeProvider
from app.llm.client import LLMClient
from app.llm.stance import ANALYST_STANCE
from app.strategy import detail_common as DC

logger = logging.getLogger(__name__)

_TOP_IND = 8            # 参与研判的行业数（资金持续流入榜头部）
_TOP_CON = 10           # 参与研判的概念数（渗透率 + 净额 两个视角并集）
_WEB_SUBJECTS = 3       # 博查联网检索的候选数（控成本/延迟）


# ──────────────────────────────────────────────
# 对外主入口
# ──────────────────────────────────────────────

def build_mainline_analysis(date: str, force: bool = False, provider: CompositeProvider | None = None) -> dict:
    """构建当日「主线板块」综合研判（资金面 + 催化剂 + 政策 → LLM 推演·非投资建议）。

    Args:
        date:     交易日 YYYYMMDD
        force:    True 时忽略缓存重算（含重新调用 LLM/联网）
        provider: 数据提供者（依赖注入·便于测试）

    Returns:
        dict：date/ind_evidence/con_evidence/news/web/analysis/disclaimer/cached。
    """
    path = DC.cache_path("mainline", date, "market")
    if not force:
        cached = DC.load_cache(path)
        if cached is not None:
            return cached

    provider = provider or CompositeProvider()
    ind_rows = _safe_industry(date)
    con_rows = _safe_concept(date, provider)
    top_ind = _rank_industries(ind_rows, _TOP_IND)
    top_con = _rank_concepts(con_rows, _TOP_CON)

    ind_evidence = [_ind_brief(r) for r in top_ind]
    con_evidence = [_con_brief(r) for r in top_con]

    keys = _keyword_set(top_ind, top_con)
    headlines = DC.macro_headlines(provider, date)
    rel_news = DC.relevant_news(headlines, keys)
    web = _web_for_candidates(top_ind, top_con)

    analysis = _compose_mainline(
        ind_text="\n".join(ind_evidence) or "（今日行业资金持续流入榜为空）",
        con_text="\n".join(con_evidence) or "（今日概念资金榜为空）",
        rel_news=rel_news,
        web=web,
    )

    data = {
        "date": date,
        "ind_evidence": [_ind_chip(r) for r in top_ind],   # 前端资金证据 chip（可点跳板块）
        "con_evidence": [_con_chip(r) for r in top_con],
        "news": rel_news,
        "web": web,
        "analysis": analysis,
        "disclaimer": ("本研判由 AI 综合「资金流向 + 公开新闻/政策」生成，是研究观点非投资建议；"
                       "资金口径为估算(非龙虎榜真钱)、催化剂以下方来源为准，请自行核查，盈亏自负。"),
        "cached": False,
    }
    DC.save_cache(path, data)
    return data


# ──────────────────────────────────────────────
# 资金面证据采集（复用已算的持续流入榜 / 渗透率榜）
# ──────────────────────────────────────────────

def _safe_industry(date: str) -> list[dict]:
    try:
        from app.strategy.industry_flow import build_industry_persistent_flow
        return build_industry_persistent_flow(date, window=10).get("rows", [])
    except Exception as e:
        logger.warning("[主线] 行业持续流入榜取数失败: %s", e)
        return []


def _safe_concept(date: str, provider: CompositeProvider) -> list[dict]:
    try:
        from app.strategy.concept_flow import build_concept_persistent_flow
        return build_concept_persistent_flow(date, window=10, provider=provider).get("rows", [])
    except Exception as e:
        logger.warning("[主线] 概念资金榜取数失败: %s", e)
        return []


def _rank_industries(rows: list[dict], k: int) -> list[dict]:
    """行业候选排序：暗流优先 → 连续流入天 → 近5累计。取头部 k 个。"""
    def score(r: dict) -> tuple:
        return (1 if r.get("ambush") else 0, r.get("consec_days") or 0, r.get("cum5") or 0)
    return sorted(rows, key=score, reverse=True)[:k]


def _rank_concepts(rows: list[dict], k: int) -> list[dict]:
    """概念候选：渗透率(相对强度)Top 与 净额Top 的并集（两个视角互补·抓小盘子猛灌+大资金主线）。"""
    by_pen = sorted([r for r in rows if isinstance(r.get("pen5"), (int, float))],
                    key=lambda r: -r["pen5"])[:k]
    by_net = sorted(rows, key=lambda r: -(r.get("cum5") if r.get("cum5") is not None else -9e9))[:k]
    seen, merged = set(), []
    for r in by_pen + by_net:                      # 渗透率优先排前
        nm = r.get("concept")
        if nm and nm not in seen:
            seen.add(nm)
            merged.append(r)
        if len(merged) >= k:
            break
    return merged


def _ind_brief(r: dict) -> str:
    tag = " [暗流:资金进价没涨]" if r.get("ambush") else (" [持续进]" if (r.get("consec_days") or 0) >= 3 else "")
    ma5 = f"·站上5日线{r['ma5']}%" if r.get("ma5") is not None else ""
    return (f"- {r.get('industry')}：近5日净{_signed(r.get('cum5'))}亿·连流{r.get('consec_days')}天"
            f"·5日涨{_pct(r.get('ret5'))}{ma5}{tag}")


def _con_brief(r: dict) -> str:
    pen = f"·渗透率{r['pen5']}%(相对强度)" if r.get("pen5") is not None else ""
    tag = " [暗流:资金进价没涨]" if r.get("ambush") else ""
    return (f"- {r.get('concept')}：近5日净{_signed(r.get('cum5'))}亿{pen}"
            f"·连流{r.get('consec_days')}天·5日涨{_pct(r.get('ret5'))}·{r.get('n')}只{tag}")


def _ind_chip(r: dict) -> dict:
    return {"name": r.get("industry"), "cum5": r.get("cum5"), "consec": r.get("consec_days"),
            "ret5": r.get("ret5"), "ambush": bool(r.get("ambush"))}


def _con_chip(r: dict) -> dict:
    return {"name": r.get("concept"), "pen5": r.get("pen5"), "cum5": r.get("cum5"),
            "consec": r.get("consec_days"), "ret5": r.get("ret5"), "ambush": bool(r.get("ambush"))}


def _keyword_set(top_ind: list[dict], top_con: list[dict]) -> set[str]:
    """从头部行业/概念名 + 领涨股构造新闻精筛关键词（杜绝 LLM 拿无关新闻附会）。"""
    keys: set[str] = set()
    for r in top_ind:
        if r.get("industry"):
            keys.add(str(r["industry"]).rstrip("ⅠⅡⅢ"))
    for r in top_con:
        if r.get("concept"):
            keys.add(str(r["concept"]))
        keys.update(DC.lead_names(r.get("lead", "")))
    return keys


def _web_for_candidates(top_ind: list[dict], top_con: list[dict]) -> list[dict]:
    """对头部候选做博查联网检索（政策/催化剂）；限 _WEB_SUBJECTS 个·去重·控成本。"""
    subjects, seen = [], set()
    for r in top_con[:2] + top_ind[:2]:            # 概念(题材性强)优先·再补行业
        nm = r.get("concept") or r.get("industry")
        if nm and nm not in seen:
            seen.add(nm)
            subjects.append(nm)
        if len(subjects) >= _WEB_SUBJECTS:
            break
    web: list[dict] = []
    for s in subjects:
        web.extend(DC.web_search(s))
    return web[:8]


# ──────────────────────────────────────────────
# 接地式 LLM 主线合成
# ──────────────────────────────────────────────

def _compose_mainline(*, ind_text: str, con_text: str, rel_news: list[str], web: list[dict]) -> str:
    """LLM(pro) 综合资金面 + 新闻 + 政策 → 主线候选研判（严禁编造·非投资建议·每候选带证伪点）。"""
    news_text = "\n".join(f"- {h}" for h in rel_news) or "（财联社电报中无头部候选直接相关条目）"
    web_text = "\n".join(
        f"- [{w.get('date', '')} {w.get('site', '')}] {w.get('title', '')}："
        f"{(w.get('summary') or w.get('snippet') or '')[:140]}"
        for w in web
    ) or "（未启用联网检索或无结果）"

    prompt = (
        "你是严谨的A股策略研究员。**只能依据下方「真实信息源」作答，严禁编造或推测任何未出现的"
        "公司、数字、事件、政策或价格。**\n\n"
        "任务：基于【资金面】（行业资金持续流入 + 概念渗透率·相对强度 + 暗流=资金进价没涨的埋伏信号）"
        "结合【相关新闻】【联网检索】，研判后市可能走强、值得跟踪埋伏的 **主线板块候选（精选 2-3 个·宁缺毋滥）**。\n\n"
        "每个候选写清三点（可连贯成段，不必生硬分点）：\n"
        "①**资金证据**——引用上方【资金面】的具体数据（净额/渗透率/连流天/暗流）；\n"
        "②**催化剂/政策**——**必须**来自【相关新闻】或【联网检索】并注明来源；若该候选只有资金没有消息面，"
        "如实写「纯资金驱动·暂无公开催化·需等题材验证」，不得自行编造利好；\n"
        "③**风险/证伪点**——什么情况说明这条主线走弱或证伪（如资金转出、冲高回落、政策不及预期）。\n\n"
        "最后用一句话点出：当前资金更像「主线确立」还是「多线博弈/轮动」。\n\n"
        f"{ANALYST_STANCE}\n"
        "**额外硬约束**：这是研究观点、不是投资建议；可给方向性研判，但**不要用「买入/抄底/追高/满仓/"
        "梭哈」等下单指令措辞**，不打包票「必涨/稳赚」；埋伏候选一律以「值得跟踪/需验证」的口吻，把决策权留给用户。\n"
        "正文之后另起一行，以「依据：」列出实际引用的信息源类别（资金面/新闻/联网）。\n\n"
        "━━ 真实信息源 ━━\n"
        f"【资金面·行业持续流入榜（申万·Tushare主力估算·近10日）】\n{ind_text}\n\n"
        f"【资金面·概念资金榜（同花顺DDE·渗透率=净流入/概念流通市值·相对强度·近10日）】\n{con_text}\n\n"
        f"【相关新闻（财联社电报·已按候选精筛）】\n{news_text}\n\n"
        f"【联网检索（博查·真实网页·含来源与日期）】\n{web_text}\n"
    )
    try:
        # pro（deepseek-reasoner）：低频高价值·夜间暖机预算·深度推理；首日交互触发约 15-20s。
        return LLMClient().chat(
            [{"role": "user", "content": prompt}],
            task_type="pro",
            temperature=0.3,
            max_tokens=2000,
        ).strip()
    except Exception as e:
        logger.warning("[主线] LLM 合成失败: %s", e)
        return "（主线研判暂不可用：LLM 调用失败，请稍后刷新重试。下方资金证据与来源仍可参考。）"


# ──────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────

def _signed(v) -> str:
    if v is None:
        return "—"
    return f"+{v}" if v >= 0 else f"{v}"


def _pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%"
