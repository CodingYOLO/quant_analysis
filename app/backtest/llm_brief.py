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
                lines.append(f"  大盘{rg}: T+5 {_hz_line(_hz(b.get('horizons', {}), 5))}")

    s = p.get("sector")
    if s and s.get("n_occ"):
        lines.append(f"【同类对比】行业「{s.get('industry')}」 同类 {s.get('n_peers')} 只 / 样本 {s.get('n_occ')} 次")
        lines.append(f"  同类基准 T+5: {_hz_line(_hz(s.get('pooled', {}), 5))}（本股见上方回测）")
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
    return "\n".join(lines)


# ──────────────────────────────────────────────
# prompt + 解析（纯函数）
# ──────────────────────────────────────────────

_SCHEMA = (
    '{\n'
    ' "stance": "<一句话定调：偏多/中性偏多/中性/中性偏谨慎/偏空 之一，必须基于数据>",\n'
    ' "supports": ["<支持该定调的理由，每条须引用具体数字>", "..."],\n'
    ' "risks": ["<矛盾点与风险，含样本量/幸存者偏差提醒>", "..."],\n'
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
        "6. 每条要点一句话、≤40字、精炼；supports 2-4 条，risks 2-4 条，todos 2-3 条。\n\n"
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
                "supports": [str(x) for x in (d.get("supports") or [])],
                "risks": [str(x) for x in (d.get("risks") or [])],
                "todos": [str(x) for x in (d.get("todos") or [])],
            }
        except Exception:
            pass
    return {"stance": "", "supports": [txt[:600]] if txt else [], "risks": [], "todos": []}


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
                      task_type="pro", max_tokens=4000, temperature=0.2)

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
