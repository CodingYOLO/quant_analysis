"""产业认知教练：给一个行业/主题，产出【数据接地】的认知卡片 + 练习题 + 答案反馈 + 自由探讨。

理念：不替你预测涨跌、不灌"风口结论"，而是用真实数据 + 网络检索的产业信息，按
"判断产业真假的6问"框架把产业讲透，并通过【你输出→AI反馈】的主动学习提升认知。

数据接地（反幻觉）：
  · 系统真实数据——行业业绩增速/资金强弱/龙头/业绩预告（因子表 + 板块强弱）
  · 博查联网检索——产业趋势/驱动/政策/龙头观点/前沿（标来源·需自行甄别）
  · 券商研报逻辑（research_hub 已有）
LLM 只做"讲解/拆解/出题/批改"，不编造数据、说不清就答"数据不足"。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from pathlib import Path

from app.config import get_settings
from app.llm.stance import ANALYST_STANCE

logger = logging.getLogger(__name__)

# 判断产业"真趋势 vs 听风就是雨"的6问框架（认知骨架）
_FRAMEWORK = (
    "判断产业真假的6问：①需求(真实放量订单/出货 vs 只有市场空间故事) "
    "②业绩(产业链公司业绩真兑现 vs 全靠未来想象) ③政策(真金白银落地 vs 一句话喊预期) "
    "④阶段(渗透率低=早期空间大 vs 已炒到天=后期接盘) ⑤资金(机构配置=趋势 vs 游资炒=情绪一日游) "
    "⑥核心环节(谁是真受益的卡脖子龙头 vs 谁只蹭概念)。"
)


def _cache(name: str, key: str) -> Path:
    d = get_settings().cache_dir / "industry_insight" / name
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def _gather_data(industry: str) -> dict:
    """从因子表+板块强弱聚合该行业的真实数据面（业绩/资金/阶段/龙头），供 LLM 接地。"""
    out: dict = {"industry": industry}
    try:
        import pandas as pd

        from app.strategy.screener import _FACTOR_TABLE_VERSION, _factor_cache_path
        files = sorted((get_settings().cache_dir / "factor_table")
                       .glob(f"*_{_FACTOR_TABLE_VERSION}.parquet"))
        if files:
            date = files[-1].name.split("_")[0]
            df = pd.read_parquet(files[-1])
            g = df[df["industry"] == industry]
            if not g.empty:
                yoy = pd.to_numeric(g.get("netprofit_yoy"), errors="coerce")
                out.update({
                    "date": date, "n": int(len(g)),
                    "avg_rps120": round(float(pd.to_numeric(g.get("rps120"), errors="coerce").mean()), 1),
                    "净利同比中位": (round(float(yoy.median()), 1) if yoy.notna().any() else None),
                    "业绩预增数": int((g.get("earn_good") == True).sum()) if "earn_good" in g else 0,  # noqa: E712
                    "龙头": [{"name": str(r["name"]),
                            "净利同比": (round(float(r["netprofit_yoy"]), 1) if pd.notna(r.get("netprofit_yoy")) else None),
                            "业绩预告": (str(r["forecast_type"]) if pd.notna(r.get("forecast_type")) else "")}
                           for _, r in g.sort_values("leader_score", ascending=False).head(5).iterrows()],
                })
        from app.strategy.sector_strength import build_sector_strength
        if files:
            sec = {s["industry"]: s for s in build_sector_strength(date).get("sectors", [])}.get(industry)
            if sec:
                out["板块判定"] = sec.get("phase")
                out["近20日涨幅"] = sec.get("avg_ret20")
    except Exception as e:
        logger.debug("行业数据聚合失败: %s", e)
    return out


def _web_research(theme: str, max_items: int = 10) -> list[dict]:
    """博查检索产业趋势/驱动/龙头观点（标题+摘要+来源URL）。失败返回空。"""
    try:
        from app.data.web_search import BochaSearchClient
        bocha = BochaSearchClient()
        if not getattr(bocha, "enabled", True):
            return []
        hits: list[dict] = []
        for q in (f"{theme} 行业 发展趋势 前景 驱动",
                  f"{theme} 龙头 竞争格局 核心环节 壁垒",
                  f"{theme} 最新 政策 订单 需求 业绩"):
            for r in bocha.search(q, count=5, freshness="oneMonth"):
                hits.append({"title": r.get("title", ""), "summary": (r.get("summary") or r.get("content") or "")[:300],
                             "url": r.get("url", ""), "site": r.get("siteName", "")})
        # 按 url 去重
        seen, uniq = set(), []
        for h in hits:
            if h["url"] and h["url"] not in seen:
                seen.add(h["url"]); uniq.append(h)
        return uniq[:max_items]
    except Exception as e:
        logger.debug("博查检索失败: %s", e)
        return []


def _facts_block(data: dict, web: list[dict]) -> str:
    lines = [f"【系统真实数据·行业「{data.get('industry')}」（数据日{data.get('date','?')}）】"]
    for k in ("n", "avg_rps120", "净利同比中位", "业绩预增数", "板块判定", "近20日涨幅"):
        if data.get(k) is not None:
            lines.append(f"- {k}: {data[k]}")
    for l in data.get("龙头", []):
        lines.append(f"- 龙头 {l['name']}：净利同比{l['净利同比']} 业绩预告{l['业绩预告']}")
    lines.append("\n【博查联网检索·产业信息（来源见URL·需甄别）】")
    for i, h in enumerate(web, 1):
        lines.append(f"[{i}] {h['title']}（{h['site']}）：{h['summary']}  {h['url']}")
    if not web:
        lines.append("（联网检索无结果，相关定性判断请显式说明'数据不足'）")
    return "\n".join(lines)


def build_insight_card(theme: str, force: bool = False, client=None) -> dict:
    """产业认知卡片（数据接地·按周缓存避免重复花费）。返回 {ok, theme, card, sources, model}。"""
    wk = datetime.date.today().strftime("%G%V")          # ISO 年+周
    cache = _cache("card", f"{hashlib.md5(theme.encode()).hexdigest()[:10]}_{wk}")
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    data = _gather_data(theme)
    web = _web_research(theme)
    prompt = (ANALYST_STANCE + "\n\n你是 A 股顶级产业研究员（实干派视角）。基于下面【真实数据+联网检索】，"
              f"把「{theme}」这个产业/主题讲透，帮投资者建立认知、不被概念忽悠。务必：\n"
              "1) 产业链结构(上游→中游→下游+核心环节/壁垒)，大白话；\n"
              f"2) 用『{_FRAMEWORK}』逐条判断：哪些是真趋势、哪些是炒作/听风就是雨；\n"
              "3) 真龙头 vs 蹭概念(结合给定龙头业绩区分)；\n"
              "4) 当前所处阶段 + 主要风险；\n"
              "**只引用给定数据/检索，不编造数字；说不清就写'数据不足'；标注关键结论的来源[n]；"
              "不预测涨跌、不给买卖建议、不输出胜率。** 用 Markdown，分小标题，控制在 800 字内。\n\n"
              f"{_facts_block(data, web)}")
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    raw = client.chat([{"role": "user", "content": prompt}], task_type="pro", max_tokens=2200, temperature=0.4)
    st = get_settings()
    model = st.claude_model if st.llm_provider == "claude" else st.deepseek_pro_model
    out = {"ok": bool(raw), "theme": theme, "card": (raw or "").strip(),
           "sources": [{"title": h["title"], "url": h["url"], "site": h["site"]} for h in web],
           "data": data, "model": model,
           "disclaimer": "认知框架+数据，非涨跌预测；联网信息需自行甄别；真正的认知靠你长期跟踪积累。"}
    if out["ok"]:
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out


def gen_quiz(theme: str, card: str, client=None) -> dict:
    """据卡片出 3-4 道苏格拉底式思考题(测真懂没·主动回忆)。返回 {ok, questions:[...]}。"""
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    prompt = ("你是产业认知教练。根据下面的产业认知卡片，出 3-4 道**思考题**，用来检验/逼出学习者对这个产业的"
              "真正理解(不是死记)。每题考点不同：如核心壁垒、真假趋势辨别、龙头逻辑、风险点。"
              "只输出 JSON 数组，每项 {\"q\":\"题目\",\"point\":\"考点一句话\"}，不要别的。\n\n卡片：\n" + card[:2500])
    raw = client.chat([{"role": "user", "content": prompt}], task_type="flash", max_tokens=700, temperature=0.5)
    qs = _parse_json_array(raw)
    return {"ok": bool(qs), "questions": (qs or [])[:4]}


def _parse_json_array(raw: str) -> list | None:
    """从 LLM 输出鲁棒提取 JSON 数组（去 ```json 围栏、取首[到尾]）。失败返回 None。"""
    import re
    if not raw:
        return None
    s = re.sub(r"```(?:json)?", "", raw).strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        out = json.loads(s[i:j + 1])
        return out if isinstance(out, list) else None
    except Exception:
        return None


def grade_answer(theme: str, card: str, question: str, answer: str, client=None) -> dict:
    """批改学习者答案：肯定对的、指出盲点、补充关键认知(费曼式反馈)。返回 {ok, feedback}。"""
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    prompt = (f"你是产业认知教练。学习者在学「{theme}」。下面是认知卡片、一道思考题、和他的回答。"
              "请给**建设性反馈**(120-220字)：先肯定答对/有洞见的点，再指出盲区或错误，最后补1个能加深认知的关键点或反问。"
              "诚实、就事论事、不灌输结论、不预测涨跌。\n\n"
              f"【卡片】{card[:1800]}\n【思考题】{question}\n【他的回答】{answer}")
    raw = client.chat([{"role": "user", "content": prompt}], task_type="pro", max_tokens=600, temperature=0.5)
    return {"ok": bool(raw), "feedback": (raw or "").strip()}


def discuss(theme: str, card: str, history: list[dict], msg: str, client=None) -> dict:
    """自由探讨：围绕该产业回答/对话(接地于卡片·可追问)。返回 {ok, reply}。"""
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    sys = (ANALYST_STANCE + f"\n你是 A 股产业研究员，正和学习者探讨「{theme}」。基于下面认知卡片作答，"
           "鼓励他独立思考、可反问；不编造数据、不预测涨跌、不给买卖建议；说不清就坦诚说不确定。\n\n卡片：\n" + card[:2200])
    msgs = [{"role": "system", "content": sys}]
    for h in (history or [])[-6:]:
        msgs.append({"role": h.get("role", "user"), "content": str(h.get("content", ""))[:1500]})
    msgs.append({"role": "user", "content": msg[:1500]})
    raw = client.chat(msgs, task_type="pro", max_tokens=900, temperature=0.6)
    return {"ok": bool(raw), "reply": (raw or "").strip()}
