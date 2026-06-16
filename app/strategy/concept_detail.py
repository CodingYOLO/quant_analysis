"""
概念详情面板：在概念资金数据之外，补充该概念的「环境 / 微观」概括分析。

对应「点击概念行展开详情」，单个概念聚合：
  1. 资金情绪定性  —— 由概念资金流行规则合成（零成本）
  2. 领涨成分股    —— 概念成分股当日涨幅 Top（ths_member 取成分 + daily 排序）
  3. 重大公告      —— 成分股当日重大公告
  4. 驱动点评      —— 接地式 LLM 简评（财联社精筛 + 博查联网，严禁编造）

通用逻辑复用 detail_common；本模块只做概念特有聚合（成分 via Tushare ths_member）。
按 (交易日, 概念) 缓存，点开才触发、同日同概念仅算一次。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.strategy import detail_common as DC
from app.strategy.concept_flow import build_concept_dashboard

logger = logging.getLogger(__name__)

_MAX_LEAD_MEMBERS = 5


# ──────────────────────────────────────────────
# 对外主入口
# ──────────────────────────────────────────────

def build_concept_detail(date: str, code: str, force: bool = False) -> dict:
    """
    构建单个概念的详情。

    Args:
        date:  交易日 YYYYMMDD
        code:  概念 Tushare 代码（如 885520.TI），来自概念资金表
        force: True 时忽略缓存重算

    Returns:
        dict，含 date/concept/code/fund/lead_members/notices/web/llm_comment/cached。

    Raises:
        ValueError: 当日无该概念数据。
    """
    path = DC.cache_path("concept", date, code)
    if not force:
        cached = DC.load_cache(path)
        if cached is not None:
            return cached

    provider = CompositeProvider()
    fund = _fund_section(date, code)
    concept_name = fund["concept"]
    symbols = _concept_member_symbols(provider, code)
    lead_members = _lead_members(provider, date, symbols)
    notices = DC.notices_for_symbols(provider, date, symbols)

    headlines = DC.macro_headlines(provider, date)
    member_names = [m["name"] for m in lead_members]
    keys = {concept_name} | set(member_names)
    rel_news = DC.relevant_news(headlines, keys)
    web = DC.web_search(f"{concept_name}概念", member_names[0] if member_names else "")

    lead_text = "、".join(f"{m['name']}({m['code']}){m['pct']:+.1f}%" for m in lead_members)
    members_text = "\n".join(f"- {m['name']}({m['code']}) {m['pct']:+.1f}%" for m in lead_members)
    llm_comment = DC.compose_comment(
        subject=f"{concept_name}概念",
        fund_summary=fund["summary"],
        lead=lead_text,
        notices=notices,
        micro_label="领涨成分股",
        micro_text=members_text,
        rel_news=rel_news,
        web=web,
    )

    data = {
        "date": date,
        "concept": concept_name,
        "code": code,
        "fund": fund,
        "lead_members": lead_members,
        "notices": notices,
        "web": web,
        "llm_comment": llm_comment,
        "cached": False,
    }
    DC.save_cache(path, data)
    return data


# ──────────────────────────────────────────────
# 概念特有聚合：资金定性 / 成分 / 领涨成分
# ──────────────────────────────────────────────

def _fund_section(date: str, code: str) -> dict:
    """从概念资金仪表盘定位本概念行，生成定性句 + 关键指标。"""
    dash = build_concept_dashboard(date)
    row = next((r for r in dash["rows"] if r["code"] == code), None)
    if row is None:
        raise ValueError(f"{date} 未找到概念代码「{code}」")

    return {
        "concept": row["concept"],
        "pct_chg": row["pct_chg"],
        "net_amount": row["net_amount"],
        "company_num": row["company_num"],
        "rank": row["rank"],
        "rank_change": row["rank_change"],
        "lead": row["lead"],
        "summary": _fund_summary_sentence(row),
    }


def _fund_summary_sentence(row: dict) -> str:
    """根据概念净额/涨跌/排名规则，合成一句资金情绪定性。"""
    net, pct, cnum, rc = row["net_amount"], row["pct_chg"], row["company_num"], row["rank_change"]
    flow_dir = "净流入" if net >= 0 else "净流出"
    if rc > 0:
        rank_txt = f"资金排名升{rc}位"
    elif rc < 0:
        rank_txt = f"资金排名降{-rc}位"
    else:
        rank_txt = "资金排名持平"

    if net > 0 and pct > 0:
        label = "资金涌入·偏强"
    elif net < 0 and pct < 0:
        label = "资金流出·偏弱"
    else:
        label = "多空分歧·震荡"

    return (
        f"{label}；概念涨跌幅{pct:+.2f}%，净额{flow_dir}{abs(net):.1f}亿，"
        f"成分{cnum}只，{rank_txt}"
    )


def _concept_member_symbols(provider: CompositeProvider, code: str) -> set[str]:
    """取概念成分股 6 位代码集合（Tushare 同花顺 ths_member）。"""
    try:
        m = provider._ts._api.ths_member(ts_code=code)
    except Exception as e:
        logger.debug("[概念详情] 成分股拉取失败 %s: %s", code, e)
        return set()
    if m is None or m.empty or "con_code" not in m.columns:
        return set()
    return {str(c).split(".")[0] for c in m["con_code"]}


def _lead_members(provider: CompositeProvider, date: str, symbols: set[str]) -> list[dict]:
    """概念成分股当日涨幅 Top（剔除新股极端值 >21%）。"""
    if not symbols:
        return []
    try:
        daily = provider.get_daily(date)
        sb = provider.get_stock_basic()
    except Exception:
        return []
    if daily is None or daily.empty:
        return []

    code2name = dict(zip(sb["symbol"].astype(str), sb["name"].astype(str)))
    d = daily.copy()
    d["_sym"] = d["ts_code"].str.split(".").str[0]
    d = d[d["_sym"].isin(symbols)]
    d["_pct"] = pd.to_numeric(d["pct_chg"], errors="coerce")
    d = d[d["_pct"] <= 21].nlargest(_MAX_LEAD_MEMBERS, "_pct")
    return [
        {"name": code2name.get(s, s), "code": s, "pct": round(float(p), 2)}
        for s, p in zip(d["_sym"], d["_pct"])
    ]
