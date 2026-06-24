"""个股360 · 综合买入判断（大脑）。

吃个股360各区【已查到的真实数据】（行情/股性/资金/板块/财务与风险/研报/最适配策略/新闻），
LLM 给出敢下判断的「该不该买」结论：结论倾向 + 综合评分 + 三档（看多依据/风险/买点止损）。
守 ANALYST_STANCE 底线：不编数据、不编胜率%、不打包票必涨、不替用户下单。

依赖注入：build_verdict 可传 fake client → 零网络单测；按 facts 指纹缓存避免重复花费。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from app.config import get_settings
from app.llm.stance import ANALYST_STANCE

logger = logging.getLogger(__name__)

_DISCLAIMER = ("以上为基于现有真实数据的综合研判，非涨跌预测、不构成投资建议；"
               "最终买卖与仓位由你决定、风险自负。")

# 各区展示顺序（缺区跳过）
_SECTION_ORDER = ["行情", "股性", "资金", "板块", "财务与风险", "研报", "最适配策略", "新闻催化"]

_SCHEMA = ('{"stance":"值得关注|观望|回避","score":<0-100整数,越高越值得关注>,'
           '"summary":"一句话结论(直给倾向与核心理由)",'
           '"bulls":["看多依据(每条带具体数字)"],'
           '"risks":["风险/不确定(每条带具体数字)"],'
           '"plan":["买点/止损/仓位参考(不是硬下单指令)"]}')


def _cache_dir() -> Path:
    d = get_settings().cache_dir / "stock_verdict"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_facts(sections: dict) -> str:
    """把各区精简文本拼成给 LLM 的事实块。sections: {区名: 文本}。"""
    parts: list[str] = []
    seen = set()
    for k in _SECTION_ORDER:
        v = (sections.get(k) or "").strip()
        if v:
            parts.append(f"【{k}】\n{v}")
            seen.add(k)
    for k, v in sections.items():          # 额外区（不在预设顺序）
        v = (str(v) or "").strip()
        if k not in seen and v:
            parts.append(f"【{k}】\n{v}")
    return "\n\n".join(parts)


def build_prompt(name: str, code: str, facts: str) -> str:
    """综合买入判断 prompt：敢下判断 + 严格 JSON。"""
    return (
        ANALYST_STANCE + "\n\n"
        f"下面是对【{name}（{code}）】聚合的【已查到的真实数据】"
        "（行情/股性/资金含真机构钱/板块/财务与风险事件/研报/最适配策略/近期新闻）。"
        "请只基于这些数据，给出『该不该买入』的综合判断，帮投资者一眼决策。\n\n"
        "要求：\n"
        "1. 只引用下方给出的数字，绝不臆造或推算新数字；样本偏小显式标「仅参考」；历史≠未来。\n"
        "2. 给**明确结论**与**综合评分(0-100)**：越高越值得关注；说清强在哪、弱在哪、关键风险。\n"
        "3. bulls(看多依据)/risks(风险) 各 3-6 条、每条带具体数字；plan(买点/止损/仓位参考) 1-3 条。\n"
        "4. 把多维矛盾串起来看：本股强但板块退潮 / 估值贵需业绩兑现 / 主力估算流入但龙虎榜机构在净卖（背离）/ "
        "有解禁减持大宗避雷 等——别只报喜。\n"
        "5. summary 一句话直给倾向与核心理由。\n"
        "6. 守底线：不编胜率%、不打包票「必涨/稳赚」、不给「全仓买入/清仓」式硬下单指令；机会与风险两面讲。\n\n"
        f"数据：\n{facts}\n\n"
        f"只输出严格的 JSON（无任何额外文字、无代码块标记、不截断），结构如下：\n{_SCHEMA}"
    )


def parse_verdict(raw: str) -> dict:
    """稳健提取 JSON；失败把原文作为 summary 兜底。"""
    txt = (raw or "").strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            return {
                "stance": str(d.get("stance", "")).strip(),
                "score": _clamp_score(d.get("score")),
                "summary": str(d.get("summary", "")).strip(),
                "bulls": [str(x) for x in (d.get("bulls") or [])],
                "risks": [str(x) for x in (d.get("risks") or [])],
                "plan": [str(x) for x in (d.get("plan") or [])],
            }
        except Exception:
            pass
    return {"stance": "", "score": None, "summary": txt[:600], "bulls": [], "risks": [], "plan": []}


def _clamp_score(v) -> int | None:
    """评分裁剪到 0-100 整数；无效返回 None。"""
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def build_verdict(name: str, code: str, sections: dict, client=None) -> dict:
    """生成个股综合买入判断。

    Args:
        name/code: 股票名称/代码。
        sections: {区名: 精简文本} —— 由调用方（页面）从各区已查数据汇总。
        client: 可注入的 LLM 客户端（便于单测）。

    Returns:
        {ok, stance, score, summary, bulls, risks, plan, model, disclaimer}
    """
    facts = build_facts(sections)
    if not facts:
        return {"ok": False, "msg": "无可用数据，无法生成判断"}

    key = hashlib.md5(f"{code}|{facts}".encode("utf-8")).hexdigest()[:16]
    cache = _cache_dir() / f"{key}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    prompt = build_prompt(name, code, facts)
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()

    def _call() -> dict:
        return parse_verdict(client.chat([{"role": "user", "content": prompt}],
                                         task_type="pro", max_tokens=8000, temperature=0.3))

    parsed = _call()
    if not parsed.get("stance"):          # 空/不可解析(多为 API 瞬时抖动或截断) → 自动重试一次
        logger.info("[verdict] %s 首次未解析出 stance，重试一次", code)
        parsed = _call()

    st = get_settings()
    model = st.claude_model if st.llm_provider == "claude" else st.deepseek_pro_model
    out = {"ok": True, **parsed, "model": model, "disclaimer": _DISCLAIMER}
    if out.get("stance"):                  # 仅缓存解析成功的结果
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out
