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


_CARD_VER = "v3"  # v3：概念型主题也解析到个股(申万行业+同花顺概念成分)·个股地图加真业绩/护城河/资金印证/证伪


def _cache(name: str, key: str) -> Path:
    d = get_settings().cache_dir / "industry_insight" / name
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def _brief_rows(g, n: int) -> list[dict]:
    """取前 n 行个股，抽成精简字典（名称/代码/市值/业绩/RPS/是否龙头）。供个股清单与 LLM 接地。"""
    import pandas as pd

    rows: list[dict] = []
    for _, r in g.head(n).iterrows():
        def num(k: str, nd: int = 1):
            v = r.get(k)
            if pd.isna(v):
                return None
            return int(round(float(v))) if nd == 0 else round(float(v), nd)
        rows.append({
            "name": str(r.get("name", "")),
            "code": (str(r["ts_code"])[:6] if pd.notna(r.get("ts_code")) else ""),
            "净利同比": num("netprofit_yoy"),
            "预告": (str(r["forecast_type"]) if pd.notna(r.get("forecast_type")) else ""),
            "预告增幅": num("forecast_chg"),
            "流通市值": num("circ_mv_100m", 0),
            "rps": num("rps120", 0),
            "主力净流入": num("main_net_amount"),         # 今日主力净流入(亿·估算超大单+大单)
            "主力3日": num("main_net_3d"),                # 近3日主力净流入(亿·估算)
            "连续流入天": num("consec_inflow", 0),         # 连续净流入天数(估算)
            "is_leader": bool(r.get("is_leader")) if pd.notna(r.get("is_leader")) else False,
        })
    return rows


def _growth_pool(g):
    """高成长池：非 ST、净利同比为正，按增速降序（扭亏/低基数会偏高，前端会提示）。"""
    import pandas as pd

    yoy = pd.to_numeric(g.get("netprofit_yoy"), errors="coerce")
    st = g.get("is_st")
    st = (pd.Series(False, index=g.index) if st is None else st.fillna(False).astype(bool))
    pool = g[yoy.notna() & (yoy > 0) & ~st]
    return pool.sort_values("netprofit_yoy", ascending=False)


def _catalyst_pool(g):
    """催化池：非 ST、有业绩预喜(预增/扭亏/略增)的，按预告净利增幅降序。"""
    import pandas as pd

    if "earn_good" not in g:
        return g.iloc[0:0]
    st = g.get("is_st")
    st = (pd.Series(False, index=g.index) if st is None else st.fillna(False).astype(bool))
    pool = g[(g["earn_good"] == True) & ~st]                             # noqa: E712
    chg = pd.to_numeric(pool.get("forecast_chg"), errors="coerce")
    return pool.assign(_chg=chg.fillna(-1e9)).sort_values("_chg", ascending=False)


def _fund_pool(g):
    """资金池：非 ST、今日主力净流入为正，按 连续流入天数→主力净流入 降序（真金进场·估算）。"""
    import pandas as pd

    net = pd.to_numeric(g.get("main_net_amount"), errors="coerce")
    st = g.get("is_st")
    st = (pd.Series(False, index=g.index) if st is None else st.fillna(False).astype(bool))
    pool = g[net.notna() & (net > 0) & ~st].copy()
    if pool.empty:
        return pool
    pool["_consec"] = pd.to_numeric(pool.get("consec_inflow"), errors="coerce").fillna(0)
    pool["_net"] = pd.to_numeric(pool.get("main_net_amount"), errors="coerce").fillna(0)
    return pool.sort_values(["_consec", "_net"], ascending=False)


def _theme_group(df, theme: str, provider):
    """把主题解析成因子表子集：先按申万行业(精确→包含)；匹配不到→按同花顺概念成分匹配。

    修复"人形机器人/AI算力/固态电池"等**概念型主题**在因子表(申万口径)查无个股→个股地图为空的问题。
    返回 (子表 g, 命中方式描述)。都匹配不到→空表。
    """
    import pandas as pd

    ind = df["industry"].astype(str)
    g = df[ind == theme]                                           # 申万行业·精确
    if not g.empty:
        return g, f"申万行业「{theme}」"
    g = df[ind.str.contains(theme, na=False, regex=False)]         # 申万行业·包含
    if not g.empty:
        return g, f"申万行业(含「{theme}」)"
    try:                                                           # 同花顺概念·成分（可并集多个别名概念）
        from app.factors.theme_wide import concept_members_map
        from app.strategy.concept_flow import _concept_member_codes_wide
        provider = provider or _new_provider()
        mmap = _concept_member_codes_wide(provider) or concept_members_map(provider)
        cand = [theme] + _THEME_ALIAS.get(theme, [])               # 俗名→同花顺概念名(光模块→CPO 等)
        keys = [k for k in cand if k in mmap]                       # 精确/别名命中
        if not keys:                                               # 退回子串包含
            sub = next((k for k in mmap if theme in k or k in theme), None)
            keys = [sub] if sub else []
        if keys:
            codes = set()
            for k in keys:
                codes |= {str(c)[:6] for c in (mmap.get(k) or [])}
            g = df[df["ts_code"].astype(str).str[:6].isin(codes)]
            if not g.empty:
                return g, f"同花顺概念「{'/'.join(keys)}」成分"
    except Exception as e:
        logger.debug("[认知] 概念成分解析失败: %s", e)
    return df.iloc[0:0], ""


# 俗名 → 同花顺概念名（俗名与同花顺口径不一致的补映射；命中的会并集取成分）
_THEME_ALIAS = {
    "光模块": ["共封装光学(CPO)", "光通信"],
    "CPO": ["共封装光学(CPO)"],
    "AI算力": ["东数西算(算力)", "算力租赁", "共封装光学(CPO)"],
    "算力": ["东数西算(算力)", "算力租赁"],
    "机器人": ["人形机器人", "减速器"],
    "半导体设备": ["半导体设备"],
}


def _new_provider():
    from app.data.composite_provider import CompositeProvider
    return CompositeProvider()


def _gather_data(industry: str, provider=None) -> dict:
    """从因子表+板块强弱聚合该行业/概念的真实数据面（业绩/资金/阶段/龙头），供 LLM 接地。

    主题解析支持 **申万行业 + 同花顺概念成分**（概念型主题也能落到个股·[[relative-strength-over-absolute]] 同源）。
    """
    out: dict = {"industry": industry}
    try:
        import pandas as pd

        from app.strategy.screener import _FACTOR_TABLE_VERSION, _factor_cache_path
        files = sorted((get_settings().cache_dir / "factor_table")
                       .glob(f"*_{_FACTOR_TABLE_VERSION}.parquet"))
        if files:
            date = files[-1].name.split("_")[0]
            df = pd.read_parquet(files[-1])
            g, via = _theme_group(df, industry, provider)          # 申万行业 或 同花顺概念成分
            out["解析口径"] = via
            if not g.empty:
                yoy = pd.to_numeric(g.get("netprofit_yoy"), errors="coerce")
                out.update({
                    "date": date, "n": int(len(g)),
                    "avg_rps120": round(float(pd.to_numeric(g.get("rps120"), errors="coerce").mean()), 1),
                    "净利同比中位": (round(float(yoy.median()), 1) if yoy.notna().any() else None),
                    "业绩预增数": int((g.get("earn_good") == True).sum()) if "earn_good" in g else 0,  # noqa: E712
                    "龙头": _brief_rows(g.sort_values("leader_score", ascending=False), 6),
                    "高成长": _brief_rows(_growth_pool(g), 6),
                    "催化": _brief_rows(_catalyst_pool(g), 6),
                    "资金流入": _brief_rows(_fund_pool(g), 6),        # 真金进场(估算)·连续流入+主力净额
                })
            # 板块阶段/涨幅：仅申万行业口径可对齐 sector_strength（概念口径跳过·不硬凑）
            if not g.empty and via.startswith("申万行业"):
                from app.strategy.sector_strength import build_sector_strength
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


def _stock_line(prefix: str, l: dict) -> str:
    """个股一行式：名称(代码) 市值 业绩 预告 RPS，None 字段自动略过。"""
    bits = [f"流通{l['流通市值']}亿" if l.get("流通市值") is not None else "",
            f"净利同比{l['净利同比']}%" if l.get("净利同比") is not None else "",
            (f"{l['预告']}" + (f"+{l['预告增幅']}%" if l.get("预告增幅") is not None else "")) if l.get("预告") else "",
            f"RPS{l['rps']}" if l.get("rps") is not None else "",
            f"主力净流入{l['主力净流入']}亿" if l.get("主力净流入") is not None else "",
            f"连续流入{l['连续流入天']}天" if l.get("连续流入天") else ""]
    return f"- {prefix} {l['name']}({l.get('code','')})：" + " ".join(b for b in bits if b)


def _facts_block(data: dict, web: list[dict]) -> str:
    via = data.get("解析口径") or "未匹配到成分"
    lines = [f"【系统真实数据·主题「{data.get('industry')}」·解析口径：{via}（数据日{data.get('date','?')}）】"]
    for k in ("n", "avg_rps120", "净利同比中位", "业绩预增数", "板块判定", "近20日涨幅"):
        if data.get(k) is not None:
            lines.append(f"- {k}: {data[k]}")
    for l in data.get("龙头", []):
        lines.append(_stock_line("龙头(强+大+活)", l))
    for l in data.get("高成长", []):
        lines.append(_stock_line("高成长(业绩增速)", l))
    for l in data.get("催化", []):
        lines.append(_stock_line("业绩催化(预喜)", l))
    for l in data.get("资金流入", []):
        lines.append(_stock_line("资金流入(真金进场·估算)", l))
    if not data.get("龙头") and not data.get("高成长"):
        lines.append("（该主题未在因子表匹配到成分股——申万行业与同花顺概念均未命中；个股地图请显式写'数据不足·未匹配到成分'，不得杜撰个股）")
    lines.append("\n【博查联网检索·产业信息（来源见URL·需甄别）】")
    for i, h in enumerate(web, 1):
        lines.append(f"[{i}] {h['title']}（{h['site']}）：{h['summary']}  {h['url']}")
    if not web:
        lines.append("（联网检索无结果，相关定性判断请显式说明'数据不足'）")
    return "\n".join(lines)


def build_insight_card(theme: str, force: bool = False, client=None) -> dict:
    """产业认知卡片（数据接地·按周缓存避免重复花费）。返回 {ok, theme, card, sources, model}。"""
    wk = datetime.date.today().strftime("%G%V")          # ISO 年+周
    cache = _cache("card", f"{hashlib.md5(theme.encode()).hexdigest()[:10]}_{_CARD_VER}_{wk}")
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
              "4) **个股地图(全卡片最重点·必须落到具体个股)**：把上面【系统真实数据】里给的"
              "龙头/高成长/催化/资金流入个股，逐个对应到产业链环节，并给**真实依据**(每只票尽量覆盖)：\n"
              "   · 产业链卡位——上游核心零部件/中游整机/下游应用，卡的什么脖子；\n"
              "   · 真业绩——引给定的净利同比/业绩预告数字(有就写、没有就说'业绩待兑现')；\n"
              "   · 护城河/壁垒——定性说明，可结合联网检索的产业信息，但**涉及具体数字只用给定数据**；\n"
              "   · 资金印证——引给定的主力净流入/连续流入天，并注明'估算·非龙虎榜真钱'；\n"
              "   · 判定——真受益龙头(有业绩+卡位+资金) vs 纯蹭概念(仅沾边无收入)，并给一句**证伪点**(什么情况说明它不是真龙头)；\n"
              "   **铁律：只能用上面给定的这些个股名，绝不许编造其它公司名或杜撰任何数字/业绩**；引用检索处标来源[n]。\n"
              "5) 当前所处阶段 + 主要风险；\n"
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


def _sys_data_line(data: dict) -> str:
    """该行业最新交易日的真实数据一行式（供问答/批改/探讨实时接地）。"""
    parts = [f"行业「{data.get('industry')}」最新数据(数据日{data.get('date', '?')})"]
    for k in ("n", "avg_rps120", "净利同比中位", "业绩预增数", "板块判定", "近20日涨幅"):
        if data.get(k) is not None:
            parts.append(f"{k}={data[k]}")
    for l in data.get("龙头", []):
        parts.append(f"龙头{l['name']}(流通{l['流通市值']}亿/净利同比{l['净利同比']}%)")
    for l in data.get("高成长", [])[:4]:
        parts.append(f"高成长{l['name']}(净利同比{l['净利同比']}%)")
    for l in data.get("资金流入", [])[:4]:
        parts.append(f"资金流入{l['name']}(主力净流入{l.get('主力净流入')}亿/连流{l.get('连续流入天')}天·估算)")
    return " · ".join(parts)


def gen_quiz(theme: str, card: str, client=None) -> dict:
    """据卡片出 3-4 道苏格拉底式思考题(测真懂没·主动回忆)。返回 {ok, questions:[...]}。"""
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    prompt = ("你是产业认知教练。根据下面的产业认知卡片，出 3-4 道**思考题**，用来检验/逼出学习者对这个产业的"
              "真正理解(不是死记)。每题考点不同：如核心壁垒、真假趋势辨别、龙头逻辑、风险点。"
              "只输出 JSON 数组，每项 {\"q\":\"题目\",\"point\":\"考点一句话\"}，不要别的。\n\n卡片：\n" + card[:2500])
    raw = client.chat([{"role": "user", "content": prompt}], task_type="pro", max_tokens=700, temperature=0.5)
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
    facts = _sys_data_line(_gather_data(theme))            # 实时重取最新交易日真实数据接地
    prompt = (f"你是产业认知教练。学习者在学「{theme}」。下面是认知卡片、最新真实数据、一道思考题、和他的回答。"
              "请给**建设性反馈**(120-220字)：先肯定答对/有洞见的点，再指出盲区或错误，最后补1个能加深认知的关键点或反问。"
              "**涉及数据请只引用下面给定的真实数据，不编造**；诚实、就事论事、不灌输结论、不预测涨跌。\n\n"
              f"【最新真实数据】{facts}\n【卡片】{card[:1700]}\n【思考题】{question}\n【他的回答】{answer}")
    raw = client.chat([{"role": "user", "content": prompt}], task_type="pro", max_tokens=600, temperature=0.5)
    return {"ok": bool(raw), "feedback": (raw or "").strip()}


def discuss(theme: str, card: str, history: list[dict], msg: str, client=None) -> dict:
    """自由探讨：围绕该产业回答/对话(接地于卡片·可追问)。返回 {ok, reply}。"""
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    facts = _sys_data_line(_gather_data(theme))            # 实时重取最新交易日真实数据接地
    sys = (ANALYST_STANCE + f"\n你是 A 股产业研究员，正和学习者探讨「{theme}」。基于下面认知卡片+最新真实数据作答，"
           "**涉及具体数字只引用给定的真实数据、绝不编造**；鼓励他独立思考、可反问；不预测涨跌、不给买卖建议；"
           f"说不清就坦诚说'数据不足/不确定'。\n\n【最新真实数据】{facts}\n\n【认知卡片】\n" + card[:2000])
    msgs = [{"role": "system", "content": sys}]
    for h in (history or [])[-6:]:
        msgs.append({"role": h.get("role", "user"), "content": str(h.get("content", ""))[:1500]})
    msgs.append({"role": "user", "content": msg[:1500]})
    raw = client.chat(msgs, task_type="pro", max_tokens=900, temperature=0.6)
    return {"ok": bool(raw), "reply": (raw or "").strip()}
