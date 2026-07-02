"""板块强弱总览：把因子表按申万行业聚合，一眼看哪个板块强势/超跌/破位 + 各板块龙头。

解决"我不知道哪个板块好、龙头是谁"——先选板块层，再下钻到龙头。
全部复用已缓存的因子表(build_factor_table)，不额外取数。判定为形态启发式，不预测涨跌。
"""

from __future__ import annotations

import pandas as pd

# —— 板块判定阈值（配置化·形态启发式·非涨跌预测）——
_RPS_STRONG = 58        # 板块平均RPS≥此 视为"前期强势"
_RPS_WEAK = 42          # ≤此 视为"弱势"
_MA60_HEALTHY = 50      # 站上MA60占比≥此% 视为"趋势未破"
_DIP5 = -3.0            # 近5日均涨幅≤此% 视为"超跌/回调"


def _sector_phase(avg_rps: float, avg_ret5: float, ma60_pct: float) -> str:
    """单板块形态判定（纯函数）：强弱 × 趋势 × 近期涨跌 → 一句话标签。"""
    strong = avg_rps >= _RPS_STRONG
    healthy = ma60_pct >= _MA60_HEALTHY
    if strong and healthy and avg_ret5 <= _DIP5:
        return "💎强势回调·可低吸"
    if strong and avg_ret5 > 0:
        return "🔥强势领涨"
    if strong:
        return "🟡强势震荡"
    if avg_rps <= _RPS_WEAK and not healthy:
        return "🔴弱势破位·避(接飞刀区)"
    return "⚪中性/震荡"


def _f(g: pd.DataFrame, col: str, how: str = "mean") -> float:
    s = pd.to_numeric(g.get(col), errors="coerce")
    if s is None or s.dropna().empty:
        return 0.0
    return float(s.mean() if how == "mean" else s.sum())


def _nz(v) -> float:
    """NaN/无效 → 0.0（切勿用 `x or 0`：NaN 为 truthy 会仍得 NaN·破坏 JSON 序列化）。"""
    x = pd.to_numeric(v, errors="coerce")
    return float(x) if pd.notna(x) else 0.0


def _aggregate_sectors(df: pd.DataFrame, min_n: int = 3, top_leaders: int = 2) -> list[dict]:
    """因子表 → 各行业强弱聚合 + 龙头（纯函数·可单测）。按板块强度(avg_rps)降序。"""
    out = []
    for ind, g in df.groupby("industry"):
        if not ind or len(g) < min_n:
            continue
        avg_rps = round(_f(g, "rps120"), 1)
        avg_ret5 = round(_f(g, "ret5"), 2)
        ma60_pct = round(pd.to_numeric(g.get("above_ma60"), errors="coerce").fillna(0).mean() * 100, 1)
        leaders = (g.sort_values("leader_score", ascending=False).head(top_leaders)
                   if "leader_score" in g.columns else g.head(top_leaders))
        out.append({
            "industry": ind, "count": int(len(g)),
            "avg_rps": avg_rps, "avg_ret5": avg_ret5,
            "avg_ret20": round(_f(g, "ret20"), 2),
            "ma60_pct": ma60_pct,
            "main_net": round(_f(g, "main_net_amount", "sum"), 2),
            "phase": _sector_phase(avg_rps, avg_ret5, ma60_pct),
            "leaders": [{"name": str(r.get("name", "")), "code": str(r.get("ts_code", "")),
                         "rps": round(_nz(r.get("rps120")), 0),      # NaN→0（防 'NaN or 0' 仍得 NaN 的泄漏）
                         "ret5": round(_nz(r.get("ret5")), 1)}
                        for _, r in leaders.iterrows()],
        })
    out.sort(key=lambda x: -x["avg_rps"])
    return out


def build_sector_strength(date: str, provider=None) -> dict:
    """读因子表(缓存) → 板块强弱榜。返回 {ok, date, sectors:[...]}。"""
    from app.strategy.screener import build_factor_table
    df = build_factor_table(date, provider)
    if df is None or df.empty:
        return {"ok": False, "msg": f"{date} 因子表为空"}
    return {"ok": True, "date": date, "sectors": _aggregate_sectors(df)}
