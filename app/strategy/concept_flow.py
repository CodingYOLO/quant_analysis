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


# ── 概念资金持续流入榜（多窗口变化 + 渗透率·相对强度·点开看成分股）──────────────────────
# 非题材归类池（次新/业绩预告类）：随财报季机械进出·非真炒作热点·从榜单剔除
_NON_THEME = ("次新股", "预增", "预减", "预亏", "预盈", "扭亏", "摘帽", "年报", "季报", "中报", "举牌")


def _is_non_theme(name: str) -> bool:
    """概念名是否为「非题材归类池」（次新股/业绩预告等·非可炒作主题）。"""
    return any(k in name for k in _NON_THEME)


def _fetch_concept_member_codes_wide(provider, cap_max: int = 1600) -> "pd.DataFrame":
    """全题材概念成分长表(concept_name/member_code)·成分数∈[5,cap_max]·剔垃圾+非题材池。

    独立于 theme_wide 的 300 上限（不影响热度看板），**覆盖大概念**(人形机器人456/机器人1204)，
    供概念「渗透率」分母（成分流通市值合计）。逐概念 ths_member·较慢·按 ISO 周缓存。
    """
    from app.factors.theme_wide import _is_junk_concept
    pro = provider._ts._api
    try:
        idx = pro.ths_index(type="N")
    except Exception as e:
        logger.warning("[概念渗透率] ths_index 失败: %s", e)
        return pd.DataFrame()
    if idx is None or idx.empty:
        return pd.DataFrame()
    idx = idx.copy()
    idx["count"] = pd.to_numeric(idx.get("count"), errors="coerce")
    idx = idx[(idx["count"] >= 5) & (idx["count"] <= cap_max)]
    idx = idx[~idx["name"].astype(str).apply(lambda s: _is_junk_concept(s) or _is_non_theme(s))]
    rows = []
    for _, r in idx.iterrows():
        name = str(r["name"])
        try:
            m = pro.ths_member(ts_code=r["ts_code"])
        except Exception:
            continue
        if m is None or m.empty or "con_code" not in m.columns:
            continue
        for con in m["con_code"]:
            rows.append({"concept_name": name, "member_code": str(con)})
    logger.info("[概念渗透率] 宽成分缓存：%d 概念 / %d 条", idx.shape[0], len(rows))
    return pd.DataFrame(rows)


def _concept_member_codes_wide(provider) -> dict:
    """{概念名: [成分ts_code]}·按 ISO 周缓存（成分变动慢）。覆盖大概念·供渗透率分母。"""
    import datetime as _dt

    from app.data.cache import cached_daily
    iso = _dt.date.today().isocalendar()
    wk = f"{iso[0]}W{iso[1]:02d}"
    df = cached_daily("concept_members_wide", wk, lambda: _fetch_concept_member_codes_wide(provider))
    if df is None or df.empty:
        return {}
    return {nm: g["member_code"].tolist() for nm, g in df.groupby("concept_name")}


def build_concept_persistent_flow(date: str, window: int = 10, provider=None) -> dict:
    """概念「资金持续流入榜」：近 window 日同花顺概念净流入 + **渗透率(净流入/概念流通市值·相对强度)**
    + 多窗口(今/1日变化/近3/3日变化/近5/近10) + 连续流入天。渗透率抓"小盘子资金猛灌"的真热点。

    ⚠️口径：同花顺概念·**成分严重重叠**→概念流通市值重复计数·**渗透率是近似**(行业口径更干净)；净流入非龙虎榜真钱。
    """
    import math

    import pandas as pd
    from app.data.cache import cached_daily
    from app.factors.theme_wide import _is_junk_concept, concept_members_map
    from app.strategy.industry_flow import _series_metrics
    provider = provider or CompositeProvider()
    pro = provider._ts._api
    dates = _recent_trade_dates(provider, date, window)
    if not dates:
        raise ValueError(f"{date} 无交易日")

    def _num(x):
        """转 float；None/NaN/±inf → None（防脏值污染累计与渗透率）。"""
        if x is None or (isinstance(x, float) and not math.isfinite(x)):
            return None
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    net_by, pct_by, comp, lead = [], [], {}, {}
    for d in dates:
        df = cached_daily("ths_concept_flow", d, lambda d=d: _fetch_concept_flow(pro, d))
        nm_net, nm_pct = {}, {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                nm = str(r.get("name", "") or "")
                if not nm or _is_junk_concept(nm) or _is_non_theme(nm):     # 剔垃圾 + 非题材归类池
                    continue
                nm_net[nm] = _num(r.get("net_amount"))
                nm_pct[nm] = _num(r.get("pct_change"))
                cn = _num(r.get("company_num"))
                if cn is not None:
                    comp[nm] = int(cn)
                if r.get("lead_stock"):
                    lead[nm] = str(r["lead_stock"])
        net_by.append(nm_net)
        pct_by.append(nm_pct)

    # 概念流通市值(渗透率分母·end日·成分circ_mv合计)：宽 map 覆盖大概念·回退窄 map
    mmap = _concept_member_codes_wide(provider) or concept_members_map(provider)
    db = provider.get_daily_basic(dates[-1])
    circ = (pd.to_numeric(db.set_index("ts_code")["circ_mv"], errors="coerce") / 1e4
            if db is not None else pd.Series(dtype=float))
    concept_circ = {nm: float(circ.reindex(codes).dropna().sum()) for nm, codes in mmap.items()}

    names = set().union(*[set(m) for m in net_by]) if net_by else set()
    rows = []
    for nm in names:
        if comp.get(nm, 0) < 5:
            continue
        nets = [m.get(nm) for m in net_by]
        if sum(1 for x in nets if x is not None) < max(5, int(window * 0.5)):
            continue
        pcts = [m.get(nm) for m in pct_by]
        met = _series_metrics(nets, pcts)                          # 复用行业多窗口指标(cum3/5/10·delta1d/3d·consec…)
        cc = concept_circ.get(nm)
        c5 = _num(met.get("cum5"))
        pen5 = (round(c5 / cc * 100, 3)
                if cc and math.isfinite(cc) and cc > 0 and c5 is not None else None)  # 渗透率%(相对强度)
        met.update({
            "concept": nm, "n": comp.get(nm, 0), "lead": lead.get(nm, ""),
            "circ": round(cc, 0) if cc and math.isfinite(cc) and cc > 0 else None,
            "pen5": pen5,
        })
        rows.append(met)
    df_out = pd.DataFrame(rows)
    if df_out.empty:
        return {"date": date, "window": len(dates), "rows": []}
    df_out = df_out.sort_values("cum5", ascending=False).reset_index(drop=True)
    df_out["rank"] = df_out.index + 1
    return {
        "date": date, "window": len(dates),
        "dates": [f"{d[4:6]}-{d[6:]}" for d in dates],
        "rows": df_out.to_dict("records"),
        "note": ("同花顺概念净流入(DDE·非龙虎榜真钱)。**渗透率%=近5日净流入/概念流通市值**(相对强度·抓小盘子猛灌)。"
                 "⚠️概念成分重叠·流通市值重复计数·渗透率近似。红=流入 绿=流出。"),
    }
