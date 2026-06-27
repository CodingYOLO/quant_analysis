"""实时资金/异动分析（纯函数·零网络·可单测）。

输入为全推快照 DataFrame（列含 ts_code/name/price/pct_chg/vol_ratio/inner/outer），
输出主动净买榜、板块资金流、资金抢筹事件、急拉（涨速）事件、持仓体检。

口径诚实：inner/outer 为 L1 内外盘（主动买卖盘估算），非龙虎榜机构真钱。
主动净买额(亿元) ≈ (外盘手 - 内盘手) × 100股 × 现价 ÷ 1e8 = (outer-inner) × price ÷ 1e6。
"""

from __future__ import annotations

import pandas as pd


def active_net_yi(inner: float, outer: float, price: float) -> float:
    """主动净买额（亿元）。外盘>内盘为主动买入净额。"""
    return round((outer - inner) * price / 1e6, 4)


def outer_ratio(inner: float, outer: float) -> float:
    """外盘占比 = 主动买 / (主动买+主动卖)；无成交返回 0.5（中性）。"""
    total = inner + outer
    return round(outer / total, 4) if total > 0 else 0.5


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """补算 net_yi / outer_ratio 两列（不改入参）。"""
    d = df.copy()
    for col in ("inner", "outer", "price", "pct_chg", "vol_ratio"):
        d[col] = pd.to_numeric(d.get(col), errors="coerce").fillna(0.0)
    d["net_yi"] = (d["outer"] - d["inner"]) * d["price"] / 1e6
    tot = d["inner"] + d["outer"]
    d["outer_ratio"] = (d["outer"] / tot.where(tot > 0)).fillna(0.5)
    return d


def fund_ranking(df: pd.DataFrame, top: int = 20) -> list[dict]:
    """主动净买额榜（降序）。过滤无内外盘数据的标的。"""
    if df is None or df.empty:
        return []
    d = _enrich(df)
    d = d[(d["inner"] + d["outer"]) > 0]
    out = []
    for r in d.nlargest(top, "net_yi").itertuples():
        out.append({"ts_code": r.ts_code, "name": str(r.name),
                    "price": round(float(r.price), 2), "pct_chg": round(float(r.pct_chg), 2),
                    "net_yi": round(float(r.net_yi), 2), "outer_ratio": round(float(r.outer_ratio), 3),
                    "vol_ratio": round(float(r.vol_ratio), 2)})
    return out


def sector_board(df: pd.DataFrame, industry_map: dict, top: int | None = None) -> list[dict]:
    """板块资金榜（按申万二级行业主动净买求和降序），每板块附【龙头=板块内主动净买最大者】。

    龙头用资金口径而非涨幅——跟主力，谁吸金最多谁是真龙头。top=None 返回全部板块。
    """
    if df is None or df.empty:
        return []
    d = _enrich(df)
    d["ind"] = d["ts_code"].map(industry_map).fillna("")
    d = d[(d["ind"] != "") & ((d["inner"] + d["outer"]) > 0)]
    if d.empty:
        return []
    rows = []
    for ind, sub in d.groupby("ind"):
        if len(sub) < 3:                          # 成分太少统计不稳
            continue
        lead = sub.nlargest(1, "net_yi").iloc[0]
        rows.append({"industry": ind, "net_yi": round(float(sub["net_yi"].sum()), 2),
                     "avg_pct": round(float(sub["pct_chg"].mean()), 2), "n": int(len(sub)),
                     "leader": str(lead["name"]), "leader_code": str(lead["ts_code"]),
                     "leader_pct": round(float(lead["pct_chg"]), 2),
                     "leader_net_yi": round(float(lead["net_yi"]), 2),
                     "leader_outer": round(float(lead["outer_ratio"]), 3)})
    rows.sort(key=lambda x: -x["net_yi"])
    return rows[:top] if top else rows


def sector_flow_events(board: list[dict], *, min_net: float = 3.0,
                       min_pct: float = 1.0) -> list[dict]:
    """板块资金事件：涌入(机会)/撤离(风险)，各带龙头。board=sector_board(全量)。

    资金口径（内外盘），与新浪 cron 的涨幅口径弱转强不重叠。
    """
    ev = []
    for s in board:
        if s["net_yi"] >= min_net and s["avg_pct"] >= min_pct:
            ev.append({**s, "kind": "in"})        # 资金涌入·机会
        elif s["net_yi"] <= -min_net and s["avg_pct"] <= -min_pct:
            ev.append({**s, "kind": "out"})       # 资金撤离·风险
    return ev


def fund_surge_events(df: pd.DataFrame, *, min_outer_ratio: float = 0.62,
                      min_vol_ratio: float = 2.0, min_pct: float = 3.0,
                      min_net_yi: float = 0.3) -> list[dict]:
    """资金抢筹个股：外盘占比高 + 放量 + 上涨 + 净买额达标（全推独有信号）。"""
    if df is None or df.empty:
        return []
    d = _enrich(df)
    mask = ((d["outer_ratio"] >= min_outer_ratio) & (d["vol_ratio"] >= min_vol_ratio)
            & (d["pct_chg"] >= min_pct) & (d["net_yi"] >= min_net_yi))
    hits = []
    for r in d[mask].nlargest(30, "net_yi").itertuples():
        hits.append({"ts_code": r.ts_code, "name": str(r.name),
                     "pct_chg": round(float(r.pct_chg), 2), "net_yi": round(float(r.net_yi), 2),
                     "outer_ratio": round(float(r.outer_ratio), 3),
                     "vol_ratio": round(float(r.vol_ratio), 2)})
    return hits


def velocity_events(now: dict, past: dict, *, min_move: float = 2.0) -> list[dict]:
    """急拉：现价相对 past 价的涨速 ≥ 阈值。now/past 均为 {ts_code: price}。"""
    out = []
    for code, p_now in now.items():
        p_old = past.get(code)
        if p_old and p_old > 0:
            move = (p_now / p_old - 1) * 100
            if move >= min_move:
                out.append({"ts_code": code, "move": round(move, 2)})
    out.sort(key=lambda x: -x["move"])
    return out


def holding_health(row: dict, stop_loss: float | None) -> tuple[str, str]:
    """持仓实时体检 → (标签, 原因)。标签: 健康 / 留意 / 风险。"""
    pct = float(row.get("pct_chg") or 0)
    o_ratio = outer_ratio(float(row.get("inner") or 0), float(row.get("outer") or 0))
    price = float(row.get("price") or 0)
    if stop_loss and price and price <= stop_loss:
        return "风险", "已触止损价"
    if pct <= -5 or o_ratio < 0.4:
        return "留意", ("急跌" if pct <= -5 else "资金转主动卖出")
    if pct >= 0 and o_ratio >= 0.55:
        return "健康", "资金主动流入"
    return "中性", "量价平稳"
