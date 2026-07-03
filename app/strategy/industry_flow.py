"""
行业板块资金流仪表盘数据聚合。

核心：一目了然资金流向——钱流入/流出了哪些板块。
产出（指定交易日）：
  - KPI：总板块数 / 平均涨跌幅 / 总上涨家数 / 总下跌家数 / 全市场主力净流向
  - 资金流向分布：按主力净流入排序的全部板块（前端取 Top10 流入 + Top10 流出）
  - 板块明细表：涨跌幅/资金净流向/超大单/上涨下跌涨停家数/排名/排名变化/领涨股

数据：全部走 CompositeProvider（daily + moneyflow + stock_basic）。
排名变化 = 今日按资金流向排名 vs 上一交易日排名。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.cache import _cache_path
from app.data.composite_provider import CompositeProvider
from app.nodes.quick_report import _board_limit_pct, _recent_trade_dates

logger = logging.getLogger(__name__)

_PERSIST_COLS = ["industry", "cum3", "cum5", "cum10", "delta1d", "delta3d",
                 "consec_days", "days_in", "n_days", "today_net", "today_pct", "ret5", "ambush", "rank"]


def _industry_agg(date: str, provider, code2name, code2ind) -> pd.DataFrame:
    """单个交易日的行业聚合表（含资金/涨跌/家数/领涨股）。"""
    daily = provider.get_daily(date)
    if daily is None or daily.empty:
        return pd.DataFrame()
    daily = daily.copy()
    daily["pct_chg"] = pd.to_numeric(daily["pct_chg"], errors="coerce")
    daily["_ind"] = daily["ts_code"].map(code2ind)
    daily = daily.dropna(subset=["_ind"])

    # 涨停判定（板块感知）
    daily["_limit_up"] = [
        p >= _board_limit_pct(ts, code2name.get(ts, "")) - 0.3 if pd.notna(p) else False
        for ts, p in zip(daily["ts_code"], daily["pct_chg"])
    ]

    # 资金流（主力净流入 + 超大单）
    mf = provider.get_money_flow(date)
    mf_map, elg_map = {}, {}
    if mf is not None and not mf.empty and "net_mf_amount" in mf.columns:
        mf = mf.copy()
        mf["main"] = (
            (mf["buy_elg_amount"] - mf["sell_elg_amount"]) +
            (mf["buy_lg_amount"] - mf["sell_lg_amount"])
        ) / 10000
        mf["elg"] = (mf["buy_elg_amount"] - mf["sell_elg_amount"]) / 10000
        daily = daily.merge(mf[["ts_code", "main", "elg"]], on="ts_code", how="left")
    else:
        daily["main"] = 0.0
        daily["elg"] = 0.0

    rows = []
    for ind, g in daily.groupby("_ind"):
        if len(g) < 3:
            continue
        up = int((g["pct_chg"] > 0).sum())
        down = int((g["pct_chg"] < 0).sum())
        # 领涨股 Top3（排除新股极端值）
        lead = g[g["pct_chg"] <= 21].nlargest(3, "pct_chg")
        lead_str = "、".join(
            f"{code2name.get(r['ts_code'], '')}({r['ts_code'].split('.')[0]}){r['pct_chg']:+.1f}%"
            for _, r in lead.iterrows()
        )
        rows.append({
            "industry": ind,
            "pct_chg": round(float(g["pct_chg"].median()), 2),
            "main_flow": round(float(g["main"].sum()), 2),
            "elg_flow": round(float(g["elg"].sum()), 2),
            "up": up, "down": down,
            "limit_up": int(g["_limit_up"].sum()),
            "count": len(g),
            "lead": lead_str,
        })
    return pd.DataFrame(rows)


def build_industry_dashboard(date: str, force: bool = False) -> dict:
    """构建行业资金流仪表盘数据（指定交易日）。"""
    provider = CompositeProvider()
    sb = provider.get_stock_basic()
    code2name = dict(zip(sb["ts_code"], sb["name"]))
    code2ind = dict(zip(sb["ts_code"], sb["industry"])) if "industry" in sb.columns else {}
    if not code2ind:
        raise ValueError("stock_basic 缺少 industry 字段")

    today_df = _industry_agg(date, provider, code2name, code2ind)
    if today_df.empty:
        raise ValueError(f"{date} 行业数据为空（收盘后约15-30分钟入库，资金流约17:40后）")

    # 上一交易日排名（用于排名变化）
    rank_change = {}
    try:
        prev_dates = _recent_trade_dates(provider, date, n=2)
        if len(prev_dates) >= 2:
            prev = prev_dates[-2]
            prev_df = _industry_agg(prev, provider, code2name, code2ind)
            if not prev_df.empty:
                prev_df = prev_df.sort_values("main_flow", ascending=False).reset_index(drop=True)
                prev_rank = {r["industry"]: i + 1 for i, r in prev_df.iterrows()}
                today_sorted = today_df.sort_values("main_flow", ascending=False).reset_index(drop=True)
                for i, r in today_sorted.iterrows():
                    pr = prev_rank.get(r["industry"])
                    rank_change[r["industry"]] = (pr - (i + 1)) if pr else 0
    except Exception as e:
        logger.debug("[行业] 排名变化计算失败: %s", e)

    # 按资金流向排序 + 注入排名/排名变化
    today_df = today_df.sort_values("main_flow", ascending=False).reset_index(drop=True)
    today_df["rank"] = today_df.index + 1
    today_df["rank_change"] = today_df["industry"].map(rank_change).fillna(0).astype(int)

    # KPI
    kpi = {
        "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "board_count": int(len(today_df)),
        "avg_pct": round(float(today_df["pct_chg"].mean()), 2),
        "total_up": int(today_df["up"].sum()),
        "total_down": int(today_df["down"].sum()),
        "total_flow": round(float(today_df["main_flow"].sum()), 2),
    }

    rows = today_df.to_dict("records")
    return {"date": date, "kpi": kpi, "rows": rows}


# ── 资金持续流入榜（多日累计 + 连续天数）──────────────────────────────────────────
def _persist_df(date: str, dates: list[str], provider) -> pd.DataFrame:
    """逐日行业聚合(复用 _industry_agg·同口径) → 每板块的近5/10日累计净流入、连续净流入天数、
    流入天数、板块近5日涨幅(复利·中位)。识别"资金进但价没涨"的暗流(ambush)。point-in-time。"""
    sb = provider.get_stock_basic()
    code2name = dict(zip(sb["ts_code"], sb["name"]))
    code2ind = dict(zip(sb["ts_code"], sb["industry"])) if "industry" in sb.columns else {}
    if not code2ind:
        raise ValueError("stock_basic 缺少 industry 字段")

    net_maps, pct_maps = [], []                                # 逐日 {行业: 主力净流入(亿)} / {行业: 中位涨幅}
    for d in dates:
        agg = _industry_agg(d, provider, code2name, code2ind)
        net_maps.append(dict(zip(agg["industry"], agg["main_flow"])) if not agg.empty else {})
        pct_maps.append(dict(zip(agg["industry"], agg["pct_chg"])) if not agg.empty else {})

    all_inds: set[str] = set().union(*[set(m) for m in net_maps]) if net_maps else set()
    rows = []
    for ind in all_inds:
        nets = [m.get(ind) for m in net_maps]                  # 亿·None=当日缺该板块
        pcts = [m.get(ind) for m in pct_maps]
        if not any(v is not None for v in nets):
            continue
        rows.append({"industry": ind, **_series_metrics(nets, pcts)})
    if not rows:
        return pd.DataFrame(columns=_PERSIST_COLS)
    df = pd.DataFrame(rows).sort_values(["cum5", "consec_days"], ascending=[False, False]).reset_index(drop=True)
    df["rank"] = df.index + 1
    return df


def _series_metrics(nets: list, pcts: list) -> dict:
    """从逐日净流入(亿)+逐日涨幅序列算持续性指标（纯函数·可测）。None=当日缺数据。

    连续净流入天数=从最新往回连续 >0 的天数；累计=非空求和；暗流=连续进≥2天且近5累计>0但价没涨(<3%)。
    """
    consec = 0
    for v in reversed(nets):
        if v is not None and v > 0:
            consec += 1
        else:
            break
    cum3 = round(sum(v for v in nets[-3:] if v is not None), 2)                 # 近3日累计
    cum5 = round(sum(v for v in nets[-5:] if v is not None), 2)
    prev3 = round(sum(v for v in nets[-6:-3] if v is not None), 2)              # 前3日(第6~4日)累计
    today_net = nets[-1] if nets else None
    y_net = nets[-2] if len(nets) >= 2 else None
    # 1日变化=今日−昨日净流入(资金加速/减速)；3日变化=近3日−前3日累计(动能转变)
    delta1d = (round(today_net - y_net, 2) if today_net is not None and y_net is not None else None)
    delta3d = round(cum3 - prev3, 2)
    ret5 = _compound_pct([p for p in pcts[-5:] if p is not None])
    return {
        "cum3": cum3, "cum5": cum5,
        "cum10": round(sum(v for v in nets if v is not None), 2),
        "delta1d": delta1d, "delta3d": delta3d,
        "consec_days": consec,
        "days_in": sum(1 for v in nets if v is not None and v > 0),
        "n_days": len(nets),
        "today_net": (round(today_net, 2) if today_net is not None else None),
        "today_pct": pcts[-1] if pcts else None,
        "ret5": ret5,
        "ambush": bool(consec >= 2 and cum5 > 0 and ret5 is not None and ret5 < 3.0),
    }


def _compound_pct(pcts: list) -> float | None:
    """复利叠加日涨幅 → 区间涨幅%(中位口径)。空→None。"""
    if not pcts:
        return None
    prod = 1.0
    for p in pcts:
        prod *= (1 + p / 100.0)
    return round((prod - 1) * 100, 2)


def build_industry_persistent_flow(date: str, force: bool = False, window: int = 10) -> dict:
    """行业「资金持续流入榜」：近 window 日主力净流入(Tushare官方口径·估算)聚合。

    ⚠️ 口径=超大单+大单代理估算·**非龙虎榜真机构钱**(真机构看 /lhb)。累计=逐日行业汇总相加。
    按日缓存；**冻结防护**：仅当最新日资金已入库(约17:40)才写缓存，避免盘中缓存到残缺数据。
    """
    provider = CompositeProvider()
    dates = _recent_trade_dates(provider, date, n=window)      # 升序·末=date·point-in-time
    if not dates:
        raise ValueError(f"{date} 无法取到交易日序列")

    path = _cache_path("industry_persist_v2", f"{date}_w{window}")   # v2: 加近3日累计+1日/3日变化
    if path.exists() and not force:
        df = pd.read_parquet(path)
    else:
        df = _persist_df(date, dates, provider)
        latest_mf = provider.get_money_flow(dates[-1])         # 最新日资金已入库才落缓存(防冻结残缺)
        if not df.empty and latest_mf is not None and not latest_mf.empty:
            df.to_parquet(path, index=False)

    return {
        "date": date, "window": len(dates),
        "dates": [f"{d[4:6]}-{d[6:]}" for d in dates],
        "rows": df.to_dict("records"),
        "note": ("口径=Tushare官方主力净流入(超大单+大单)代理估算·非龙虎榜真机构钱(真钱看机构动向)；"
                 "累计=近N日逐日行业汇总相加。红=流入 绿=流出。暗流=资金连续进但板块价没涨(埋伏)。"),
    }
