"""
回测·AI 综合研判（DeepSeek 最强推理 v4-pro）。

把个股回测页【已算好的结构化结果】(回测胜率/大盘状态分桶/同类对比/板块广度/股性/筹码/基本面)
喂给最强推理模型，输出一段接地的综合研判：定调 + 支持理由(带数字) + 矛盾/风险 + 待确认。

红线（prompt 强约束）：只解读不预测、不生成新"胜率/概率"、不给买卖指令、每条引用具体数字、
小样本必提示。模型是真相的"解读者"，量化引擎才是真相源（符合 CLAUDE.md 禁止 LLM 输出胜率排序）。
依赖注入：generate_brief 可传入 fake client，便于零网络单测；按 facts 指纹缓存避免重复花费。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

_DISCLAIMER = "以上为基于历史回测数据的研判，非涨跌预测、不构成投资建议；历史回测≠未来收益。"


def _cache_dir() -> Path:
    d = get_settings().cache_dir / "brief"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ──────────────────────────────────────────────
# facts 构建（纯函数）
# ──────────────────────────────────────────────

def _hz(horizons: dict, h: int) -> dict:
    """取持有期统计，兼容 int / str 键（json 往返后变 str）。"""
    return (horizons or {}).get(h) or (horizons or {}).get(str(h)) or {}


def _hz_line(s: dict) -> str:
    """单个持有期统计 → 一行可读文本。"""
    if not s or not s.get("n"):
        return "样本不足"
    wr = round(s["win_rate"] * 100) if s.get("win_rate") is not None else "—"
    return f"胜率{wr}% 均收益{s.get('avg_return', 0):+}% 盈亏比{s.get('profit_factor', '—')} (n={s['n']})"


def build_facts(p: dict) -> str:
    """把回测/大盘/同类/板块/股性/基本面结构化结果压成数字密集的事实块。"""
    r = p.get("result") or {}
    lines = [f"【回测】信号「{r.get('signal_label', '')}」 区间 {r.get('start')}~{r.get('end')} "
             f"共 {r.get('n_signals', 0)} 次信号"]
    hz = r.get("horizons") or {}
    for h in (1, 3, 5, 10):
        lines.append(f"  T+{h}: {_hz_line(_hz(hz, h))}")

    byr = r.get("by_regime") or {}
    if byr:
        rw = r.get("regime_window") or {}
        lines.append(f"【大盘状态·{r.get('index_label', '')}】当前 {r.get('current_regime', '—')}；"
                     f"区间占比 强势{rw.get('强势', 0)}%/震荡{rw.get('震荡', 0)}%/弱势{rw.get('弱势', 0)}%")
        for rg in ("强势", "震荡", "弱势"):
            b = byr.get(rg)
            if b and b.get("n"):
                hh = b.get("horizons", {})
                lines.append(f"  大盘{rg}: T+3 {_hz_line(_hz(hh, 3))}；T+5 {_hz_line(_hz(hh, 5))}")

    s = p.get("sector")
    if s and s.get("n_occ"):
        lines.append(f"【同类对比】行业「{s.get('industry')}」 同类 {s.get('n_peers')} 只 / 样本 {s.get('n_occ')} 次")
        pl = s.get("pooled", {})
        lines.append(f"  同类基准 T+3 {_hz_line(_hz(pl, 3))}；T+5 {_hz_line(_hz(pl, 5))}（本股见上方回测）")
        cb = s.get("current_breadth") or {}
        lines.append(f"【板块广度】当前 站上MA20 {cb.get('pct_ma20')}% / 站上MA5 {cb.get('pct_ma5')}%")
        for lbl, b in (s.get("by_breadth") or {}).items():
            if b.get("n"):
                lines.append(f"  {lbl}: T+5 {_hz_line(_hz(b.get('horizons', {}), 5))}")

    prof = p.get("profile") or {}
    tags = [t.get("text") for t in (prof.get("tags") or []) if t.get("text")]
    if tags:
        lines.append("【股性】" + "、".join(tags))
    chip = prof.get("chip") or {}
    if chip.get("weight_avg") is not None:
        lines.append(f"【筹码】主力平均成本 {chip.get('weight_avg')}、现价 {chip.get('ref_close')}、"
                     f"溢价 {chip.get('premium')}%、获利盘 {chip.get('winner_rate')}%")

    fund = p.get("fundamentals") or {}
    if fund.get("summary"):
        lines.append("【基本面】" + str(fund.get("summary")))
    fc = fund.get("forecast")
    if fc:
        lines.append(f"【业绩预告】{fc.get('type', '')} {fc.get('net_change', '')}（{fc.get('period', '')}）")
    sv = fund.get("survey")
    if sv:
        lines.append(f"【机构调研热度】近90天 {sv.get('count_90d')} 次、近180天 {sv.get('count_180d')} 次"
                     f"（关注度{sv.get('heat')}）")
    an = fund.get("analyst")
    if an and an.get("ok"):
        rt = "、".join(f"{k}{v}" for k, v in (an.get("ratings") or {}).items())
        lines.append(f"【券商盈利预测】目标价均值 {an.get('target_avg')}（区间 {an.get('target_low')}~"
                     f"{an.get('target_high')}）、覆盖 {an.get('n_org')} 家、评级 {rt}")
    ev = fund.get("events") or {}
    if ev:
        bits = []
        fl = ev.get("float")
        if fl:
            bits.append(f"下次解禁 {fl['next_date']}（距今{fl['next_days']}天，比例{fl['next_ratio']}，"
                        f"未来{fl['upcoming_count']}场）")
        ht = ev.get("holder_trade")
        if ht:
            lt = ht["latest"]
            bits.append(f"近180天股东减持{ht['de_count']}次/增持{ht['in_count']}次，"
                        f"最近{lt['date']}{lt['holder']}{lt['type']}{lt.get('ratio') or ''}%")
        ex = ev.get("express")
        if ex:
            bits.append(f"业绩快报{ex['period']} 净利{ex.get('net_profit_yi')}亿(同比{ex.get('net_profit_yoy')}%)")
        hn = ev.get("holdernum")
        if hn:
            bits.append(f"股东户数{hn['latest']}（{hn.get('trend') or ''}）")
        if bits:
            lines.append("【事件/避雷面(已取官方数据，请据此直接核查解禁/减持，不要再列为待确认)】" + "；".join(bits))

    news = p.get("news") or {}
    if news.get("summary"):
        lines.append("【近期新闻(博查实测·供核查消息面/解禁/减持/政策)】\n" + str(news["summary"]).strip())
    return "\n".join(lines)


# ──────────────────────────────────────────────
# prompt + 解析（纯函数）
# ──────────────────────────────────────────────

_SCHEMA = (
    '{\n'
    ' "stance": "<一句话定调：偏多/中性偏多/中性/中性偏谨慎/偏空 之一，必须基于数据>",\n'
    ' "summary": "<2-4 句综合研判正文：把回测/大盘状态/同类/板块广度/基本面串起来，'
    '讲清当下这只票+这个打法该不该做、为什么；可有层次、引用数字>",\n'
    ' "supports": ["<支持该定调的理由，每条1-2句、引用具体数字>", "..."],\n'
    ' "risks": ["<矛盾点与风险，每条1-2句、含样本量/幸存者偏差提醒>", "..."],\n'
    ' "todos": ["<还需人工确认什么，如业绩雷/解禁/消息面>", "..."]\n'
    '}'
)


def build_prompt(name: str, code: str, signal_label: str, facts: str) -> str:
    """综合研判 prompt：强约束红线 + 严格 JSON 输出。"""
    return (
        f"你是严谨的A股量化研判助手。下面是对【{name}（{code}）】用「{signal_label}」做的历史回测"
        "与板块分析的【已经算好的结构化结果】。请只基于这些数据做综合研判，帮助投资者判断"
        "“这只票 + 这个打法在当前环境下值不值得做”。\n\n"
        "严格红线（违反即视为失败）：\n"
        "1. 只能引用下方给出的数字，绝不臆造或推算任何新数字；\n"
        "2. 绝不输出新的“胜率/成功率/概率”，绝不预测涨跌幅或目标价；\n"
        "3. 绝不给出“买入/卖出/加仓/清仓”等交易指令，只做研判与风险提示；\n"
        "4. 每条理由与风险都要引用具体数字；若样本偏小（同类样本<50、单票信号<10、或某分桶<10）"
        "必须显式提示“样本薄、仅供参考”；\n"
        "5. 要把多个维度串起来看矛盾（如回测胜率高但盈亏比低=胜小亏大；本股强但板块退潮）。\n"
        "6. 力求专业、有深度：supports 3-6 条、risks 3-6 条、todos 1-3 条；每条 1-2 句把依据讲透"
        "（带数字），不必强行精简；summary 写 2-4 句连贯总评。\n"
        "7. 数据若含【同类对比】，直接给出“本股是个股 alpha 还是板块共性”的结论并写入支持/风险，"
        "不要把它列为待确认。\n"
        "8. 数据若含【近期新闻】，据此主动核查消息面/解禁/减持/政策并写入分析（注明依据新闻）；"
        "todos 只保留‘无法从给定数据判断、确需人工再查’的事项——能从数据/新闻里得出结论的，绝不踢回给用户。\n\n"
        f"数据：\n{facts}\n\n"
        f"只输出严格的 JSON（不要任何额外文字、不要代码块标记、不要省略号截断），结构如下：\n{_SCHEMA}"
    )


def parse_brief(raw: str) -> dict:
    """从模型输出中稳健提取 JSON；失败则把原文作为单条支持项兜底。"""
    txt = (raw or "").strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            return {
                "stance": str(d.get("stance", "")).strip(),
                "summary": str(d.get("summary", "")).strip(),
                "supports": [str(x) for x in (d.get("supports") or [])],
                "risks": [str(x) for x in (d.get("risks") or [])],
                "todos": [str(x) for x in (d.get("todos") or [])],
            }
        except Exception:
            pass
    return {"stance": "", "summary": "", "supports": [txt[:800]] if txt else [], "risks": [], "todos": []}


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

def generate_brief(payload: dict, client=None) -> dict:
    """
    生成综合研判。payload: {name, signal_label, result(必填), sector?, profile?, fundamentals?}。
    client 可注入（便于单测）；按 facts 指纹缓存避免重复花费。
    """
    r = payload.get("result") or {}
    if not r or not r.get("n_signals"):
        return {"ok": False, "msg": "请先跑一次回测再生成研判"}

    facts = build_facts(payload)
    key = hashlib.md5(facts.encode("utf-8")).hexdigest()[:16]
    cache = _cache_dir() / f"{key}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    name = payload.get("name") or r.get("ts_code", "")
    prompt = build_prompt(name, r.get("ts_code", ""),
                          payload.get("signal_label") or r.get("signal_label", ""), facts)
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    raw = client.chat([{"role": "user", "content": prompt}],
                      task_type="pro", max_tokens=8000, temperature=0.2)

    st = get_settings()
    model = st.claude_model if st.llm_provider == "claude" else st.deepseek_pro_model
    out = {"ok": True, **parse_brief(raw), "model": model, "disclaimer": _DISCLAIMER}
    # 仅缓存解析成功的结果（stance 非空），避免把截断/异常输出缓存下来
    if out.get("stance"):
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out
