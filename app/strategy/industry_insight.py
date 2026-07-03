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


_CARD_VER = "v5"  # v5：非策展产业也用 LLM 从成分归类核心卡位龙头(带环节)·覆盖任意产业·而非通用 leader_score


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


# 主题 → tech_chain 产业链名（手工梳理的核心龙头篮子·比 leader_score 更懂"卡位"）
_THEME_CHAIN = {
    "人形机器人": "具身智能·机器人", "机器人": "具身智能·机器人", "具身智能": "具身智能·机器人",
    "减速器": "具身智能·机器人", "丝杠": "具身智能·机器人", "执行器": "具身智能·机器人",
    "AI算力": "AI算力·芯片", "算力": "AI算力·芯片", "半导体": "AI算力·芯片", "芯片": "AI算力·芯片",
    "光模块": "AI算力·光/PCB/连接", "CPO": "AI算力·光/PCB/连接", "光通信": "AI算力·光/PCB/连接",
    "服务器": "AI算力·服务器/散热", "液冷": "AI算力·服务器/散热", "散热": "AI算力·服务器/散热",
    "稀土永磁": "稀土永磁", "稀土": "稀土永磁", "磁材": "稀土永磁", "小金属": "有色·小金属",
    "军工": "军工", "国防军工": "军工", "航空": "军工", "航天": "军工", "军船": "军工",
    "消费电子": "消费电子", "苹果": "消费电子", "果链": "消费电子", "AI手机": "消费电子",
    "光伏": "光伏", "组件": "光伏", "逆变器": "光伏", "光伏概念": "光伏",
    "PCB": "PCB", "覆铜板": "PCB", "载板": "PCB", "PCB概念": "PCB",
    "医疗器械": "医疗器械", "医疗设备": "医疗器械", "IVD": "医疗器械", "医疗器械概念": "医疗器械",
}


def _chain_core(df, theme: str) -> list[dict]:
    """主题命中 tech_chain → 返回**手工梳理的产业链核心龙头**(带「环节」·按上游→下游排序)，从因子表补真实数据。

    解决"leader_score 只按 强+大+活 排序，把光纤/存储巨头顶上来、真核心零部件公司(三花智控/埃斯顿)沉底"的问题。
    未命中 tech_chain → 返回空(调用方回退 leader_score)。
    """
    try:
        from app.strategy import tech_chain as TC
        name = _THEME_CHAIN.get(theme) or next((n for n in TC.chain_names() if theme and theme in n), None)
        chain = TC._CHAIN_BY_NAME.get(name) if name else None
        if not chain:
            return []
        seen, ordered = set(), []                              # 按 上游→中游→下游 收集·去重(留最核心环节)
        for layer in chain.get("layers", []):
            for node in layer.get("nodes", []):
                for _lname, code in node.get("leaders", []):
                    c6 = str(code)[:6]
                    if c6 not in seen:
                        seen.add(c6)
                        ordered.append((c6, node.get("name", "")))
        code6 = df["ts_code"].astype(str).str[:6]
        rows: list[dict] = []
        for c6, seg in ordered:
            sub = df[code6 == c6]
            if sub.empty:                                      # 该龙头不在因子表(停牌/退市)→跳过
                continue
            br = _brief_rows(sub, 1)[0]
            br["环节"] = seg                                    # 减速器/丝杠·执行器/无框电机/力传感器/本体…
            rows.append(br)
        return rows
    except Exception as e:
        logger.debug("[认知] 产业链核心龙头解析失败: %s", e)
        return []


def _llm_core_leaders(df, theme: str, g=None, client=None) -> list[dict]:
    """**无 tech_chain 覆盖的产业**：LLM 做产业链结构科普(核心环节+代表龙头)→与因子表按名匹配·补真实数据·周缓存。

    让"产业链核心龙头"质量覆盖**任意产业**(不止手工策展的少数链)。
    关键：用"产业链科普(客观知识·非荐股)"式问法——直接"从成分名单挑龙头"会触发国产模型的荐股合规过滤返回空。
    接地：LLM 报出的公司名与**因子表按名严格匹配**，只保留真实 A 股(剔除幻觉/非A股/港美股)并补真实业绩+资金。
    按 ISO 周缓存，同题同周只调一次 LLM。
    """
    import pandas as pd
    cache = _cache("chain_leaders", f"{hashlib.md5(theme.encode()).hexdigest()[:10]}_{datetime.date.today().strftime('%G%V')}")
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    # ⚠️措辞极敏感：国产模型对"列公司"类指令有荐股合规过滤(某些措辞如"龙头公司"/全角标点/"投资建议"会返回空)。
    # 下面这版极简·半角标点·科普口吻·实测稳定返回。改动务必重测(_llm_core_leaders 返回空=被过滤)。
    prompt = (f"你在做产业链结构科普(客观知识·非荐股)。梳理「{theme}」产业链核心环节(上游→下游),"
              "每环节列1-3家代表性A股上市公司。只输出JSON数组,每项{\"环节\":\"..\",\"公司\":\"..\"}。")
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    try:
        raw = client.chat([{"role": "user", "content": prompt}], task_type="flash", max_tokens=1000, temperature=0.3)
    except Exception as e:
        logger.warning("[认知] LLM 产业链科普失败: %s", e)
        return []
    names = df["name"].astype(str)
    seen, rows = set(), []
    for it in (_parse_json_array(raw) or []):
        comp = str(it.get("公司", "") or "").strip()
        seg = str(it.get("环节", "") or "").strip()
        if not comp:
            continue
        sub = df[names == comp]                                         # 精确名匹配(接地·剔幻觉/非A股)
        if sub.empty and len(comp) >= 3:
            sub = df[names.str.contains(comp, na=False, regex=False)]   # 退回包含
        if sub.empty:
            continue
        c6 = str(sub.iloc[0]["ts_code"])[:6]
        if c6 in seen:
            continue
        seen.add(c6)
        br = _brief_rows(sub.head(1), 1)[0]
        br["环节"] = seg
        rows.append(br)
        if len(rows) >= 10:
            break
    try:                                                               # 空结果也缓存(过滤确定性·避免同题同周重复调LLM)
        cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return rows


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
            core = _chain_core(df, industry)                        # ①策展核心龙头(tech_chain·带环节·最优)
            core_src = "chain" if core else ""
            if not core and not g.empty and len(g) >= 10:           # ②非策展产业→LLM 从成分挑核心(覆盖任意产业)
                core = _llm_core_leaders(df, industry, g)
                core_src = "llm" if core else ""
            out["核心口径"] = bool(core)
            out["核心来源"] = core_src                               # chain(策展) / llm(AI归类) / ''(回退因子)
            if not g.empty:
                yoy = pd.to_numeric(g.get("netprofit_yoy"), errors="coerce")
                out.update({
                    "date": date, "n": int(len(g)),
                    "avg_rps120": round(float(pd.to_numeric(g.get("rps120"), errors="coerce").mean()), 1),
                    "净利同比中位": (round(float(yoy.median()), 1) if yoy.notna().any() else None),
                    "业绩预增数": int((g.get("earn_good") == True).sum()) if "earn_good" in g else 0,  # noqa: E712
                    # 龙头：命中产业链→用手工梳理的核心卡位公司(带环节)；否则回退 leader_score(强+大+活)
                    "龙头": core if core else _brief_rows(g.sort_values("leader_score", ascending=False), 6),
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
    seg = f"[{l['环节']}]" if l.get("环节") else ""              # 产业链环节(减速器/丝杠·执行器…)
    bits = [f"流通{l['流通市值']}亿" if l.get("流通市值") is not None else "",
            f"净利同比{l['净利同比']}%" if l.get("净利同比") is not None else "",
            (f"{l['预告']}" + (f"+{l['预告增幅']}%" if l.get("预告增幅") is not None else "")) if l.get("预告") else "",
            f"RPS{l['rps']}" if l.get("rps") is not None else "",
            f"主力净流入{l['主力净流入']}亿" if l.get("主力净流入") is not None else "",
            f"连续流入{l['连续流入天']}天" if l.get("连续流入天") else ""]
    return f"- {prefix}{seg} {l['name']}({l.get('code','')})：" + " ".join(b for b in bits if b)


def _facts_block(data: dict, web: list[dict]) -> str:
    via = data.get("解析口径") or "未匹配到成分"
    lines = [f"【系统真实数据·主题「{data.get('industry')}」·解析口径：{via}（数据日{data.get('date','?')}）】"]
    for k in ("n", "avg_rps120", "净利同比中位", "业绩预增数", "板块判定", "近20日涨幅"):
        if data.get(k) is not None:
            lines.append(f"- {k}: {data[k]}")
    lead_tag = {"chain": "产业链核心龙头(手工梳理·卡位)",
                "llm": "产业链核心龙头(AI从成分归类·卡位)"}.get(data.get("核心来源"), "龙头(强+大+活·因子)")
    for l in data.get("龙头", []):
        lines.append(_stock_line(lead_tag, l))
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
              "龙头/高成长/催化/资金流入个股，逐个对应到产业链环节，并给**真实依据**(每只票尽量覆盖)。"
              "**其中标了『产业链核心龙头』的是按卡位手工梳理的真核心(带[环节]标签如[减速器][丝杠·执行器])，"
              "务必优先、重点讲这些**；『强+大+活·因子』类只是市值大/涨得好，未必是本产业核心，需甄别其是否只是沾边；\n"
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
