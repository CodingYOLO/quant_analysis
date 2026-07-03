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


# ── 概念板块·多日资金流动特征（供板块诊断"资金层"嗅题材热点·纯描述非信号）──────────────
def _cross_z_map(m: dict) -> dict:
    """某日概念净额的横截面稳健 z-score（中位/MAD）·让概念强度可比。<5→全None。"""
    import numpy as np
    vals = [v for v in m.values() if v is not None]
    if len(vals) < 5:
        return {k: None for k in m}
    a = np.array(vals, dtype=float)
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med))) * 1.4826 or (float(a.std()) or 1.0)
    # 裁剪 ±4：概念数量多、离群极端·防个别概念 z 爆表主导升温榜(与行业可比)
    return {k: (round(max(-4.0, min(4.0, (v - med) / mad)), 2) if v is not None else None)
            for k, v in m.items()}


def build_concept_flow_features(end: str, window: int = 14, provider=None, min_company: int = 5) -> list[dict]:
    """概念板块近 window 日资金流动特征（同花顺 moneyflow_cnt_ths 官方净额·亿）。

    纯描述·**嗅当下题材热点**（机器人/CPO/减速器等·不在申万L2里）·非回测信号（不受成分漂移限制）。
    返回与行业统一 schema 的 flow 行：{sector,kind='概念',net5,penz_seq,pen_accel,flow_margin,ret5,n}。
    """
    from app.data.cache import cached_daily
    from app.factors.theme_wide import _is_junk_concept
    from app.strategy.sector_attribution import _compound_pct, _margin
    from app.strategy.sector_diagnosis import _smooth_seq
    provider = provider or CompositeProvider()
    pro = provider._ts._api
    dates = _recent_trade_dates(provider, end, window)
    if not dates:
        return []

    net_by, pct_by, comp = [], [], {}
    for d in dates:
        df = cached_daily("ths_concept_flow", d, lambda d=d: _fetch_concept_flow(pro, d))  # 按日缓存
        nmap, pmap = {}, {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                nm = str(r.get("name", "") or "")
                if not nm or nm == "nan" or _is_junk_concept(nm):
                    continue
                na, pc, cn = r.get("net_amount"), r.get("pct_change"), r.get("company_num")
                nmap[nm] = float(na) if pd.notna(na) else None
                pmap[nm] = float(pc) if pd.notna(pc) else None
                if pd.notna(cn):
                    comp[nm] = int(cn)
        net_by.append(nmap)
        pct_by.append(pmap)

    z_by = [_cross_z_map(m) for m in net_by]
    names = set().union(*[set(m) for m in net_by]) if net_by else set()
    need = max(5, int(window * 0.5))
    rows = []
    for nm in names:
        if comp.get(nm, 0) < min_company:                          # 剔太小的概念(成分<min)
            continue
        nets = [m.get(nm) for m in net_by]
        if sum(1 for x in nets if x is not None) < need:            # 数据太少跳过
            continue
        zfull = [m.get(nm) for m in z_by]
        accel = (round(zfull[-1] - zfull[-2], 2)
                 if zfull[-1] is not None and zfull[-2] is not None else None)
        pcts = [m.get(nm) for m in pct_by]
        f5d = round(sum(x for x in nets[-5:] if x is not None), 1)
        ret5 = _compound_pct([p for p in pcts[-5:] if p is not None])
        f1d = next((x for x in reversed(nets) if x is not None), None)
        rows.append({
            "sector": nm, "kind": "概念",
            "net5": f5d, "penz_seq": _smooth_seq(zfull, 3, 5), "pen_accel": accel,
            "flow_margin": _margin(nets), "ret5": ret5, "n": comp.get(nm, 0),
            "f1d": round(f1d, 1) if f1d is not None else None,
            "net_seq": [round(x, 1) if x is not None else None for x in nets[-5:]],
            "ma5": None,                                            # 概念无成分宽度
            "ambush": bool(f5d > 0 and (ret5 or 0) < 3),           # 资金进+价没涨=暗流
        })
    return rows
