"""
概念板块资金流仪表盘数据聚合（同花顺概念口径）。

与 industry_flow（Tushare 110 行业，自聚合）互补：
本模块直连 Tushare 官方接口 moneyflow_cnt_ths（同花顺概念资金流），
一次调用即返回每个概念的涨跌幅 / 净额 / 领涨股 / 成分数，
不依赖被封的东方财富概念接口，国内服务器可直连。

产出（指定交易日）：
  - KPI：概念数 / 平均涨跌幅 / 净流入概念数 / 净流出概念数 / 全市场概念净额
  - 概念明细：按净额排序，含涨跌幅 / 净额 / 成分数 / 领涨股 / 排名 / 排名变化

数据：走 CompositeProvider 内的 Tushare pro_api（与 market_extras 同一约定）。
单位：net_amount 为 Tushare 官方口径「净额（亿元）」。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.nodes.quick_report import _recent_trade_dates

logger = logging.getLogger(__name__)


def _fetch_concept_flow(pro, date: str) -> pd.DataFrame:
    """拉取并规范化单个交易日的同花顺概念资金流。空表返回空 DataFrame。"""
    try:
        df = pro.moneyflow_cnt_ths(trade_date=date)
    except Exception as e:
        logger.warning("[概念] moneyflow_cnt_ths 拉取失败: %s", e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    for col in ("pct_change", "net_amount", "company_num", "pct_change_stock"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _build_rows(df: pd.DataFrame, rank_change: dict[str, int]) -> list[dict]:
    """将规范化后的概念资金流表转为前端行记录（按净额降序）。"""
    df = df.sort_values("net_amount", ascending=False).reset_index(drop=True)
    rows = []
    for i, r in df.iterrows():
        name = str(r["name"])
        lead = str(r.get("lead_stock", "") or "")
        lead_pct = r.get("pct_change_stock")
        lead_str = f"{lead} {lead_pct:+.1f}%" if lead and pd.notna(lead_pct) else lead
        rows.append({
            "concept": name,
            "code": str(r["ts_code"]),
            "pct_chg": round(float(r["pct_change"]), 2) if pd.notna(r["pct_change"]) else 0.0,
            "net_amount": round(float(r["net_amount"]), 2) if pd.notna(r["net_amount"]) else 0.0,
            "company_num": int(r["company_num"]) if pd.notna(r.get("company_num")) else 0,
            "lead": lead_str,
            "rank": i + 1,
            "rank_change": int(rank_change.get(name, 0)),
        })
    return rows


def _rank_change_map(provider, pro, date: str, today_names_order: list[str]) -> dict[str, int]:
    """计算各概念今日 vs 上一交易日的净额排名变化（正=排名上升）。"""
    try:
        prev_dates = _recent_trade_dates(provider, date, n=2)
        if len(prev_dates) < 2:
            return {}
        prev_df = _fetch_concept_flow(pro, prev_dates[-2])
        if prev_df.empty:
            return {}
        prev_df = prev_df.sort_values("net_amount", ascending=False).reset_index(drop=True)
        prev_rank = {str(r["name"]): i + 1 for i, r in prev_df.iterrows()}
        return {
            name: (prev_rank[name] - (i + 1))
            for i, name in enumerate(today_names_order)
            if name in prev_rank
        }
    except Exception as e:
        logger.debug("[概念] 排名变化计算失败: %s", e)
        return {}


def build_concept_dashboard(date: str) -> dict:
    """
    构建概念资金流仪表盘数据（指定交易日）。

    Args:
        date: 交易日 YYYYMMDD

    Returns:
        {"date", "kpi": {...}, "rows": [...]}，结构对齐 industry_flow 便于前端复用。

    Raises:
        ValueError: 当日无概念资金流数据（非交易日或数据未入库）。
    """
    provider = CompositeProvider()
    pro = provider._ts._api

    df = _fetch_concept_flow(pro, date)
    if df.empty:
        raise ValueError(f"{date} 概念资金流为空（非交易日，或收盘后数据尚未入库）")

    sorted_names = df.sort_values("net_amount", ascending=False)["name"].astype(str).tolist()
    rank_change = _rank_change_map(provider, pro, date, sorted_names)
    rows = _build_rows(df, rank_change)

    net = df["net_amount"]
    kpi = {
        "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "concept_count": int(len(df)),
        "avg_pct": round(float(df["pct_change"].mean()), 2),
        "inflow_count": int((net > 0).sum()),
        "outflow_count": int((net < 0).sum()),
        "total_net": round(float(net.sum()), 2),
    }
    return {"date": date, "kpi": kpi, "rows": rows}
