"""讲解模块（2026-08-02 用户定档：**讲解不是结论**）。

设计约束（违反即模块失败）：
· 结论型（"今天偏松·可以积极些"）会让用户跳过数据看结论·手感永远建立不起来——禁止；
· 讲解型（"X 处于什么位置 + 这个位置在传导链条上意味着什么"）是教用户怎么读——唯一允许的形态；
· 摘要只放页面**底部**（用户先自己读一遍数据形成判断·讲解做校正而非替代）；
· 回看模式同样可生成（输入是该日 point-in-time 行·回看 20240924 这类关键日是最高效的学习方式）。

三个功能：
  score_breakdown  「为什么是 64?」——纯数学拆解·不用 LLM
  daily_explain    「今天发生了什么」——LLM·3-5句·禁词硬校验
  (卡片级「这是什么」在 registry.EXPLAIN·静态·也不用 LLM)
"""

from __future__ import annotations

import logging

from app.macro import store

logger = logging.getLogger(__name__)

# 用户点名的禁用词（出现任何一个 = 越界成结论型/建议型 → 重试一次·仍犯则如实报错）
FORBIDDEN = ("建议", "应该", "可以考虑", "看多", "看空", "加仓", "减仓", "机会",
             "风险偏好上升", "风险偏好下降")


# ──────────────────────────────────────────────
# ①「为什么是 64?」——分层得分拆解（纯计算）
# ──────────────────────────────────────────────

def score_breakdown(layer: str, date: str) -> dict:
    """把某层得分拆到指标：谁拉高、谁拉低、权重各多少。

    数学恒等式：score = Σ(adj×w)/Σw = 50 + Σ pull，其中
    pull_i = (adj_i − 50) × w_i / Σw —— 每个指标相对"中性50"的署名贡献，
    Σpull 精确等于 score−50，用户能核账。
    """
    from app.macro.service import _adj, _score_state
    d = date
    metas = [m for m in store.get_meta() if m["layer"] == layer]
    rows = store.read_panel(d)
    part, excluded = [], []
    for m in metas:
        row = rows.get(m["code"])
        if row is None:
            excluded.append({"code": m["code"], "name": m["name_cn"], "reason": "当日无数据"})
            continue
        ok, why = _score_state(m, row, d)
        if not ok:
            excluded.append({"code": m["code"], "name": m["name_cn"],
                             "reason": why or "未参与"})
            continue
        part.append((m, row))
    if not part:
        return {"ok": True, "layer": layer, "date": d, "score": None,
                "items": [], "excluded": excluded, "note": "该层当日无参与评分的指标"}

    wsum = sum(m["weight"] for m, _ in part)
    items = []
    for m, row in part:
        adj = _adj(row["pctile_750"], int(m["direction"]))
        items.append({
            "code": m["code"], "name": m["name_cn"], "direction": int(m["direction"]),
            "pctile": row["pctile_750"], "adj": adj,
            "weight": m["weight"], "weight_pct": round(m["weight"] / wsum * 100, 1),
            # 相对中性50的署名贡献：>0 拉高·<0 拉低
            "pull": round((adj - 50) * m["weight"] / wsum, 2),
        })
    items.sort(key=lambda x: -x["pull"])
    score = round(50 + sum(x["pull"] for x in items), 2)
    return {"ok": True, "layer": layer, "label": store.LAYER_LABELS.get(layer, layer),
            "date": d, "score": score, "items": items, "excluded": excluded,
            "note": "score = 50 + Σ各指标拉动(拉动=(好坏分位−50)×权重占比)·可逐项核账"}


# ──────────────────────────────────────────────
# ②「今天发生了什么」——LLM 讲解（禁词硬校验）
# ──────────────────────────────────────────────

# ⚠️空输出排查实录（2026-08-02·当时误判为荐股过滤·实测纠正）：真凶是 **deepseek-v4-flash
# 对数据分析类任务自动进入思考模式**——finish_reason=length·reasoning_content 1041字·
# content 0字：max_tokens=700 全被思考吃光·正文没开始写就截断。偶发"成功"只是那次思考短。
# 修法：max_tokens 给足(2500) + 空content且length截断时加倍重试。禁词清单仍**只放输出侧**
# 校验(validate_text)——prompt 里列敏感词没必要冒过滤风险。
# 讲解质量定档(2026-08-02 用户反馈"太模糊太晦涩·学不到东西")：
# 套话式传导("收紧金融条件")只报通道名不讲因果·必须逐步走链并落到"对流入流出A股的钱"上。
_SYSTEM = """你是一位宏观数据讲解老师。读者是一位正在学习独立分析宏观数据的普通投资者，不熟悉术语。
任务：从当天数据里挑 3-4 条最重要的信息，每条讲透。每条固定用这四段结构（保留【】标题）：

【现象】指标名=数值（分位X，即比过去三年约X%的日子更高/更低）——一句白话说这个数是高是低、罕不罕见。
【怎么传导】把因果链完整写出来，形如"A高了 → 所以B → 于是C → 最终D"。每一步都要说清"为什么
会导致下一步"，像给完全外行讲一样，不许跳步。可以打比方。
【落到A股的钱上】明确说这条链的终点对"进出A股的钱"意味着什么：是哪类钱（外资、融资盘、
场外基金申购、居民存款搬家）、走什么通道、变多还是变少、大概快慢（当天就反应还是要几个月）。
只描述机制，不给任何操作含义。
【配合看】指出面板上应同时看哪1-2个其它指标来验证这条链，并说"如果链条成立，那个指标应该长什么样"。

硬性要求：
1. 禁用模糊套话："收紧/放松金融条件""风险偏好""流动性环境""承压""边际"——必须换成具体白话；
   说"资金"必须说清是哪里的资金（银行间的钱/股市里的钱/居民的钱）。
2. 数字只能来自输入数据，不能编造。
3. 挑选优先级：标了异动的 > 分位≥90或≤10的 > 传导链路上的关键堵点。
4. 属于教学讲解，不涉及任何交易或资产配置内容。
5. 措辞规范（最重要·输出前逐词自查）：全程只用**描述句**。以下词语一个都不能出现：
   建议、应该、可以考虑、看多、看空、加仓、减仓、机会、风险偏好上升、风险偏好下降。
   替代写法：指示句"应该关注X"→描述句"X 是这条链的验证点"；"存在机会/风险"→"历史上
   这种位置通常伴随…"；涉及仓位买卖的内容一律不写。【配合看】固定句式
   "可观察X；若此链成立，X 通常呈现…"。
6. 直接输出，条与条之间空一行，不要开场白和结尾语。"""


def daily_explain(date: str, force: bool = False) -> dict:
    """生成（或读缓存）某日讲解。回看日期同样可用——输入本就是该日 point-in-time 行。"""
    store.init_db()
    from app.macro.service import resolve_date
    d = resolve_date(date)
    if not d:
        return {"ok": False, "error": "宏观库为空"}
    cached = store.read_summary(d)
    if cached and cached.get("explain") and not force:
        return {"ok": True, "date": d, "text": cached["explain"],
                "model": cached.get("model", ""), "cached": True}

    payload = _build_llm_input(d)
    text, model = _call_llm(payload)
    bad = [w for w in FORBIDDEN if w in text]
    if bad:                                        # 越界 → 带着违规反馈重试一次
        logger.warning("[macro] 讲解命中禁词 %s·重试", bad)
        # 重试反馈不回传违规词本身(避免把敏感词注入上下文)·只描述违规类型
        text, model = _call_llm(payload + "\n\n注意：上一版含有指示/建议式措辞或仓位类词汇，"
                                          "违反了措辞规范第5条。全部改为纯描述句重写。")
        bad = [w for w in FORBIDDEN if w in text]
        if bad:
            return {"ok": False, "date": d,
                    "error": f"LLM 两次输出均含禁词{bad}·按纪律不展示(讲解不是结论)"}
    store.upsert_summary(d, "", "", "", model)
    _save_explain(d, text)
    return {"ok": True, "date": d, "text": text, "model": model, "cached": False}


def _build_llm_input(d: str) -> str:
    """当日全量指标态 → 紧凑输入。只含库内数据·不联网。"""
    metas = {m["code"]: m for m in store.get_meta()}
    rows = store.read_panel(d)
    lines = [f"日期 {d}",
             "[传导链路参考] 美联储路径(美债2Y)→中美利差→离岸人民币→LPR政策空间→银行间利率；"
             "社融/M1(信用扩张)→银行间FDR007(银行间的钱贵不贵)→两市成交额(股市里的钱活跃度)；"
             "融资余额/市值(杠杆盘)与宽基ETF申购(场外的钱)→两市成交额"]
    for r in store.read_scores(d):
        if r["score"] is not None:
            lines.append(f"[层] {r['layer']} 得分{r['score']} ({r['n_part']}/{r['n_total']}参与)")
    for code, m in metas.items():
        row = rows.get(code)
        if not row or row.get("value") is None:
            continue
        seg = (f"{m['name_cn']}={row['value']}{m['unit'] or ''}"
               f" 分位{row['pctile_750'] if row['pctile_750'] is not None else '样本不足'}"
               f" 1日变动{row['chg_1d'] if row['chg_1d'] is not None else '—'}")
        if row.get("anomaly"):
            seg += f" ⚡异动{'↑' if row['anomaly'] > 0 else '↓'}"
        if int(m["direction"]) == -1:
            seg += "（高=偏空口径）"
        elif int(m["direction"]) == 0:
            seg += "（中性口径）"
        lines.append(seg)
    return "\n".join(lines)


def _call_llm(user_input: str) -> tuple[str, str]:
    from app.llm.client import LLMClient
    client = LLMClient()
    # ⭐关闭思考模式(实测 thinking:disabled 有效·reasoning=0)：v4-flash 对四段结构任务
    # 思考失控膨胀(7900字·把全文在思考里起草一遍)·加预算到16000也要跑5-10分钟——按钮不可用。
    # 关思考后秒级返回·预算只需覆盖正文(四段×4条≈1200字)。留2次重试防偶发空。
    for max_tok in (2000, 4000):
        text = client.chat(
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user_input}],
            task_type="flash", temperature=0.1, max_tokens=max_tok,
            extra_body={"thinking": {"type": "disabled"}},
        )
        text = (text or "").strip()
        if text:
            return text, getattr(client, "_last_model", "") or "flash"
        logger.warning("[macro] 讲解返回空(max_tokens=%d)·加倍重试", max_tok)
    return "", "flash"


def _save_explain(d: str, text: str) -> None:
    with store._conn() as con:
        con.execute("UPDATE macro_summary SET explain=? WHERE trade_date=?", (text, d))


def validate_text(text: str) -> list[str]:
    """禁词检查（供测试与外部复用）。"""
    return [w for w in FORBIDDEN if w in text]
