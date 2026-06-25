"""个股360 · 公司画像：这家公司到底是干什么的 + 有没有护城河。

设计纪律（与「数据必须可溯源」一致）：
- **硬事实**（主营业务/主营构成/员工/行业）走 Tushare 官方数据，零编造、直接展示。
- **行业地位/全球排名/核心竞争力**这类软判断走「博查联网检索 + LLM 归纳」，
  每条结论必须有检索来源[n]；没有依据就老实写"公开资料未明确"，
  **绝不臆想排名/市占率数字**。地位/排名标为"需自行甄别"，非投资建议。

依赖注入：build_company_profile 可传 fake client → 零网络单测；按 ts_code+月缓存避免重复花费。
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path

from app.config import get_settings
from app.llm.stance import ANALYST_STANCE

logger = logging.getLogger(__name__)

_VER = "v1"
_DISCLAIMER = ("主营业务/主营构成/员工为 Tushare 官方数据；行业地位/排名/竞争力为基于公开检索的归纳，"
               "可能滞后或不全、请按来源自行核实；非涨跌预测、不构成投资建议。")


def _cache_path(ts_code: str) -> Path:
    d = get_settings().cache_dir / "company_profile"
    d.mkdir(parents=True, exist_ok=True)
    ym = datetime.date.today().strftime("%Y%m")          # 公司画像变动慢→按月缓存
    return d / f"{ts_code}_{_VER}_{ym}.json"


def _fmt_period(end_date: str) -> str:
    y, md = end_date[:4], end_date[4:]
    return {"1231": f"{y}年报", "0930": f"{y}三季报",
            "0630": f"{y}中报", "0331": f"{y}一季报"}.get(md, end_date)


def _fmt_yi(v):
    """元 → 亿元，保留 2 位；无效返回 None。"""
    try:
        return round(float(v) / 1e8, 2)
    except (TypeError, ValueError):
        return None


def _gather_facts(ts_code: str, name: str, provider) -> dict:
    """Tushare 硬事实：主营业务/简介/员工/主营构成(产品+占比)/行业。"""
    import pandas as pd

    out: dict = {"name": name, "ts_code": ts_code}
    try:
        c = provider.get_stock_company(ts_code)
        if c is not None and not c.empty:
            r = c.iloc[0]
            out["主营业务"] = str(r.get("main_business") or "").strip()
            out["简介"] = str(r.get("introduction") or "").strip()[:320]
            emp = pd.to_numeric(r.get("employees"), errors="coerce")
            out["员工数"] = int(emp) if pd.notna(emp) else None
    except Exception as e:
        logger.debug("[company] stock_company 取数失败: %s", e)

    try:
        mb = provider.get_main_business(ts_code)
        if mb is not None and not mb.empty:
            mb = mb.copy()
            mb["end_date"] = mb["end_date"].astype(str)
            latest = mb["end_date"].max()
            g = mb[mb["end_date"] == latest].copy()
            g["_s"] = pd.to_numeric(g["bz_sales"], errors="coerce")
            total = float(g["_s"].sum()) if g["_s"].notna().any() else 0.0
            comp = []
            for _, r in g.sort_values("_s", ascending=False).head(8).iterrows():
                s = r["_s"]
                comp.append({
                    "产品": str(r.get("bz_item") or ""),
                    "营收亿": _fmt_yi(s),
                    "占比": (round(float(s) / total * 100, 1) if total and pd.notna(s) else None),
                })
            out["主营构成"] = comp
            out["构成报告期"] = _fmt_period(latest)
    except Exception as e:
        logger.debug("[company] fina_mainbz 取数失败: %s", e)

    try:
        sb = provider.get_stock_basic()
        h = sb[sb["ts_code"] == ts_code]
        if not h.empty:
            out["行业"] = str(h.iloc[0].get("industry") or "")
    except Exception:
        pass
    return out


def _web_research(name: str, max_items: int = 9) -> list[dict]:
    """博查检索公司的行业地位/排名/竞争力/客户（标题+摘要+来源URL）。失败返回空。"""
    try:
        from app.data.web_search import BochaSearchClient
        bocha = BochaSearchClient()
        if not getattr(bocha, "enabled", True):
            return []
        hits: list[dict] = []
        for q in (f"{name} 行业地位 市占率 全球排名",
                  f"{name} 核心竞争力 技术壁垒 护城河 主要客户",
                  f"{name} 主要产品 竞争对手 龙头"):
            for r in bocha.search(q, count=5, freshness="oneYear"):
                hits.append({"title": r.get("title", ""),
                             "summary": (r.get("summary") or r.get("content") or "")[:280],
                             "url": r.get("url", ""), "site": r.get("siteName", ""),
                             "date": (r.get("datePublished") or r.get("dateLastCrawled") or "")[:10]})
        seen, uniq = set(), []
        for h in hits:
            if h["url"] and h["url"] not in seen:
                seen.add(h["url"])
                uniq.append(h)
        return uniq[:max_items]
    except Exception as e:
        logger.debug("[company] 博查检索失败: %s", e)
        return []


def _facts_block(facts: dict, web: list[dict]) -> str:
    lines = [f"【Tushare 官方硬数据·{facts.get('name')}（{facts.get('ts_code')}）】"]
    if facts.get("行业"):
        lines.append(f"- 申万行业: {facts['行业']}")
    if facts.get("主营业务"):
        lines.append(f"- 主营业务(工商登记): {facts['主营业务']}")
    if facts.get("简介"):
        lines.append(f"- 公司简介: {facts['简介']}")
    if facts.get("员工数"):
        lines.append(f"- 员工数: {facts['员工数']}")
    if facts.get("主营构成"):
        lines.append(f"- 主营构成(报告期{facts.get('构成报告期', '')}·按产品):")
        for c in facts["主营构成"]:
            lines.append(f"    · {c['产品']}: 营收{c['营收亿']}亿 占比{c['占比']}%")
    lines.append("\n【博查联网检索·行业地位/排名/竞争力（来源见URL·需甄别）】")
    for i, h in enumerate(web, 1):
        lines.append(f"[{i}] {h['title']}（{h['site']} {h.get('date', '')}）：{h['summary']}  {h['url']}")
    if not web:
        lines.append("（联网检索无结果，地位/排名类请显式写'公开资料未明确'）")
    return "\n".join(lines)


def _parse_obj(raw: str) -> dict | None:
    """从 LLM 输出稳健提取首个 JSON 对象（去 ```json 围栏）。失败返回 None。"""
    if not raw:
        return None
    s = re.sub(r"```(?:json)?", "", raw)
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _build_prompt(name: str, facts: dict, web: list[dict]) -> str:
    return (ANALYST_STANCE + "\n\n你是 A股产业研究员。基于下面【Tushare硬数据 + 博查检索】，"
            f"把「{name}」这家公司讲清楚，让投资者一眼明白它是干什么的、行不行。只输出 JSON：\n"
            '{"定位":"一句话说清它在产业链什么位置、靠什么赚钱",'
            '"行业地位":"国内/行业地位+依据[n]；无据写\'公开资料未明确\'",'
            '"全球排名":"有明确公开排名才给(带[n])；否则写\'公开资料未见明确全球排名\'，绝不编数字",'
            '"核心竞争力":["护城河(技术/客户/规模/壁垒，附依据[n]或注明来自简介)","..."],'
            '"局限与风险":["客户集中/技术差距/周期性/被卡脖子等(就事论事)"]}\n'
            "**铁律**：①主营/产品占比只能用上面的 Tushare 数据；②地位/排名/市占率这类，"
            "**必须有检索[n]支撑，没有就老实写\'公开资料未明确\'，绝不臆想排名或市占率数字**；"
            "③区分硬数据(Tushare)与联网信息(需甄别)；④不预测涨跌、不荐买卖。\n\n"
            f"{_facts_block(facts, web)}")


def build_company_profile(ts_code: str, name: str, provider=None, client=None) -> dict:
    """公司画像：硬事实(主营/构成) + LLM 归纳(地位/排名/护城河·带来源)。按月缓存。

    Returns:
        {ok, name, ts_code, 主营业务, 简介, 员工数, 行业, 主营构成, 构成报告期,
         定位, 行业地位, 全球排名, 核心竞争力, 局限与风险, sources, model, disclaimer}
    """
    if provider is None:
        from app.data.composite_provider import CompositeProvider
        provider = CompositeProvider()

    cache = _cache_path(ts_code)
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    facts = _gather_facts(ts_code, name, provider)
    if not facts.get("主营业务") and not facts.get("主营构成"):
        return {"ok": False, "msg": "暂无公司主营数据"}

    web = _web_research(name)
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    raw = client.chat([{"role": "user", "content": _build_prompt(name, facts, web)}],
                      task_type="pro", max_tokens=4000, temperature=0.3)
    llm = _parse_obj(raw) or {}

    st = get_settings()
    model = st.claude_model if st.llm_provider == "claude" else st.deepseek_pro_model
    out = {
        "ok": True, "name": name, "ts_code": ts_code,
        "主营业务": facts.get("主营业务", ""), "简介": facts.get("简介", ""),
        "员工数": facts.get("员工数"), "行业": facts.get("行业", ""),
        "主营构成": facts.get("主营构成", []), "构成报告期": facts.get("构成报告期", ""),
        "定位": str(llm.get("定位", "")).strip(),
        "行业地位": str(llm.get("行业地位", "")).strip(),
        "全球排名": str(llm.get("全球排名", "")).strip(),
        "核心竞争力": [str(x) for x in (llm.get("核心竞争力") or [])],
        "局限与风险": [str(x) for x in (llm.get("局限与风险") or [])],
        "sources": [{"title": h["title"], "url": h["url"], "site": h["site"], "date": h.get("date", "")}
                    for h in web],
        "model": model, "disclaimer": _DISCLAIMER,
    }
    if out["定位"] or out["核心竞争力"]:        # 仅在 LLM 归纳成功时缓存，失败下次重试
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out
