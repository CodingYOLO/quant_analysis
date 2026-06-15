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

from app.data.composite_provider import CompositeProvider
from app.nodes.quick_report import _board_limit_pct, _recent_trade_dates

logger = logging.getLogger(__name__)


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
