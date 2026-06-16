"""
行业详情面板：在资金数据之外，补充该行业的「环境 / 宏观 / 微观」概括分析。

面向「点击行业行展开详情」的按需场景，单个行业聚合四类信息：
  1. 资金情绪定性  —— 规则合成（复用 industry_flow 的聚合结果，零成本）
  2. 重大公告      —— 该行业成分股当日重大公告（akshare 公告 → 代码映射行业）
  3. 关联活跃题材  —— 领涨概念中归属本行业的题材（复用 concept_flow）
  4. 驱动点评      —— 接地式 LLM 简评（财联社精筛新闻 + 博查联网检索，严禁编造）

通用逻辑（公告/新闻/联网/点评/缓存）下沉到 detail_common，本模块只做行业特有聚合。
成本控制：整份详情按 (交易日, 行业) 缓存，点开才触发、同日同行业仅算一次。
"""

from __future__ import annotations

import logging

from app.data.composite_provider import CompositeProvider
from app.strategy import detail_common as DC
from app.strategy.industry_flow import build_industry_dashboard

logger = logging.getLogger(__name__)

_MAX_THEMES = 5


# ──────────────────────────────────────────────
# 对外主入口
# ──────────────────────────────────────────────

def build_industry_detail(date: str, industry: str, force: bool = False) -> dict:
    """
    构建单个行业的详情（资金定性 / 公告 / 题材 / 联网 / LLM 驱动点评）。

    Args:
        date:     交易日 YYYYMMDD
        industry: Tushare 行业名（与资金表的 industry 列一致）
        force:    True 时忽略缓存重新生成（含重新调用 LLM/联网）

    Returns:
        dict，含 date/industry/fund/notices/themes/web/llm_comment/cached。

    Raises:
        ValueError: 当日无该行业数据。
    """
    path = DC.cache_path("industry", date, industry)
    if not force:
        cached = DC.load_cache(path)
        if cached is not None:
            return cached

    provider = CompositeProvider()
    fund = _fund_section(date, industry)
    members = _industry_members(provider, industry)
    notices = DC.notices_for_symbols(provider, date, members["symbols"])
    themes = _themes_section(date, industry, provider)

    headlines = DC.macro_headlines(provider, date)
    keys = {industry} | set(DC.lead_names(fund["lead"])) | {t["concept"] for t in themes}
    rel_news = DC.relevant_news(headlines, keys)
    leads = DC.lead_names(fund["lead"])
    web = DC.web_search(f"{industry}行业", leads[0] if leads else "")

    themes_text = "\n".join(
        f"- {t['concept']}（净{t['net_amount']:+.0f}亿，领涨{t['lead']}）" for t in themes
    )
    llm_comment = DC.compose_comment(
        subject=f"{industry}行业",
        fund_summary=fund["summary"],
        lead=fund["lead"],
        notices=notices,
        micro_label="关联活跃题材",
        micro_text=themes_text,
        rel_news=rel_news,
        web=web,
    )

    data = {
        "date": date,
        "industry": industry,
        "fund": fund,
        "notices": notices,
        "themes": themes,
        "web": web,
        "llm_comment": llm_comment,
        "cached": False,
    }
    DC.save_cache(path, data)
    return data


# ──────────────────────────────────────────────
# 行业特有聚合：资金定性 / 成分股 / 关联题材
# ──────────────────────────────────────────────

def _fund_section(date: str, industry: str) -> dict:
    """从行业资金仪表盘定位本行业行，生成定性句 + 关键指标。"""
    dash = build_industry_dashboard(date)
    row = next((r for r in dash["rows"] if r["industry"] == industry), None)
    if row is None:
        raise ValueError(f"{date} 未找到行业「{industry}」（行业名需与资金表一致）")

    return {
        "pct_chg": row["pct_chg"],
        "main_flow": row["main_flow"],
        "limit_up": row["limit_up"],
        "up": row["up"],
        "down": row["down"],
        "rank": row["rank"],
        "rank_change": row["rank_change"],
        "lead": row["lead"],
        "count": row["count"],
        "summary": _fund_summary_sentence(row),
    }


def _fund_summary_sentence(row: dict) -> str:
    """根据资金/涨跌/排名规则，合成一句资金情绪定性。"""
    mf, lu, up, down, rc, pct = (
        row["main_flow"], row["limit_up"], row["up"], row["down"],
        row["rank_change"], row["pct_chg"],
    )
    flow_dir = "净流入" if mf >= 0 else "净流出"
    if rc > 0:
        rank_txt = f"资金排名升{rc}位"
    elif rc < 0:
        rank_txt = f"资金排名降{-rc}位"
    else:
        rank_txt = "资金排名持平"

    if mf > 0 and up > down and lu > 0:
        label = "资金轮入·偏强"
    elif mf < 0 and down >= up:
        label = "资金撤离·偏弱"
    else:
        label = "多空分歧·震荡"

    return (
        f"{label}；涨跌幅{pct:+.2f}%，主力{flow_dir}{abs(mf):.1f}亿，"
        f"涨停{lu}家，{up}涨/{down}跌，{rank_txt}"
    )


def _industry_members(provider: CompositeProvider, industry: str) -> dict:
    """返回该行业成分股 {symbols: set(6位代码), ts_codes: set(ts_code)}。"""
    sb = provider.get_stock_basic()
    sub = sb[sb["industry"] == industry]
    return {
        "symbols": set(sub["symbol"].astype(str)),
        "ts_codes": set(sub["ts_code"].astype(str)),
    }


def _themes_section(date: str, industry: str, provider: CompositeProvider) -> list[dict]:
    """从同花顺概念资金流中，挑出领涨股归属本行业的活跃概念作为关联题材。"""
    try:
        from app.strategy.concept_flow import build_concept_dashboard
        dash = build_concept_dashboard(date)
    except Exception as e:
        logger.debug("[行业详情] 概念数据失败: %s", e)
        return []

    sb = provider.get_stock_basic()
    name2ind = dict(zip(sb["name"].astype(str), sb["industry"].astype(str)))

    themes = []
    for r in dash["rows"]:
        lead_name = str(r.get("lead", "")).split(" ")[0]  # "中航沈飞 +7.2%" → "中航沈飞"
        if lead_name and name2ind.get(lead_name) == industry:
            themes.append({
                "concept": r["concept"],
                "net_amount": r["net_amount"],
                "pct_chg": r["pct_chg"],
                "lead": r["lead"],
            })
        if len(themes) >= _MAX_THEMES:
            break
    return themes
