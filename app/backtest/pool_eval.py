"""
选股池评分回测/验证（B4）：用真数据回答"重点分到底灵不灵"。

两条互补路径：
- A 历史回测（`run_historical`）：从全市场前复权面板一次性算**价格结构评分**
  （RPS 强度 + 乖离/追高/高位风险调整，**不含资金/板块/筹码**——这些无历史），
  对过去 N 个采样交易日，把股票按评分分「强/中/弱」三档，统计各档 T+5 实际胜率/均收益。
  若"强档"持续跑赢"弱档" → 评分有效。马上有结果（约束：缺资金/板块/筹码因子）。
- B 前向验证（`run_forward`）：从**真实选股池**（含完整评分）逐日聚合 ⭐/高分/低分的
  T+5 表现。完整 100% 评分，但需 T+5 到期后逐日累积。

诚实：历史回测是"价格结构"近似（板块热度等无历史）；前向才是完整评分的真实战绩。
口径：买入＝信号次日收盘，卖出＝T+5 收盘（防未来函数）。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.strategy.stock_pool import _RISK_BIAS, _RISK_CHASE, _RISK_HIGH, _RISK_MAX

logger = logging.getLogger(__name__)

HORIZON = 5          # T+5
_WIN_RPS = 50        # RPS 回看交易日
_WARMUP = 60         # MA60/RPS 预热
_MIN_PRICE = 2.0     # 剔除仙股
_MIN_VALID = 30      # 当日有效股太少则跳过


def _ramp_vec(v: pd.Series, lo: float, hi: float) -> pd.Series:
    return ((v - lo) / (hi - lo)).clip(0.0, 1.0)


def _stats(returns: pd.Series) -> dict:
    """一组 T+N 收益(%) → {n, win_rate, avg_return}。"""
    r = returns.dropna()
    n = int(len(r))
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_return": None}
    return {"n": n, "win_rate": round(float((r > 0).mean() * 100), 1),
            "avg_return": round(float(r.mean()), 2)}


# ──────────────────────────────────────────────
# A 历史回测（价格结构评分·全市场面板）
# ──────────────────────────────────────────────

def _price_score(panel: pd.DataFrame, cols: list[str], i: int) -> pd.DataFrame:
    """在面板第 i 列(交易日)算每只股的价格结构评分 + 因子。返回有效股 DataFrame。"""
    cur = panel[cols[i]]
    ma5 = panel[cols[i - 4:i + 1]].mean(axis=1)
    ma10 = panel[cols[i - 9:i + 1]].mean(axis=1)
    ma20 = panel[cols[i - 19:i + 1]].mean(axis=1)
    ma60 = panel[cols[i - 59:i + 1]].mean(axis=1)
    high120 = panel[cols[max(0, i - 119):i + 1]].max(axis=1)

    df = pd.DataFrame({"cur": cur, "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60})
    df["bias20"] = (cur - ma20) / ma20 * 100
    df["change_7d"] = (cur / panel[cols[i - 7]] - 1) * 100
    df["dist_high"] = (cur / high120 - 1) * 100
    df["ret50"] = cur / panel[cols[i - _WIN_RPS]] - 1
    df = df[(df["cur"] > _MIN_PRICE) & df["ret50"].notna() & df["ma60"].notna()]
    if df.empty:
        return df

    df["rps"] = df["ret50"].rank(pct=True) * 100
    a5, a10 = df["cur"] >= df["ma5"], df["cur"] >= df["ma10"]
    a20, a60 = df["cur"] >= df["ma20"], df["cur"] >= df["ma60"]
    bull = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"])
    df["ma_score"] = np.select(
        [a5 & a10 & a20 & a60 & bull, a5 & a10 & a20 & bull, a5 & a10, a20, a60],
        [1.0, 0.85, 0.65, 0.4, 0.15], default=0.0)
    risk = (_ramp_vec(df["bias20"], *_RISK_BIAS[:2]) * _RISK_BIAS[2]
            + _ramp_vec(df["change_7d"], *_RISK_CHASE[:2]) * _RISK_CHASE[2]
            + _ramp_vec(df["dist_high"], *_RISK_HIGH[:2]) * _RISK_HIGH[2]).clip(0, _RISK_MAX)
    df["score"] = df["rps"] + df["ma_score"] * 10 - risk      # 价格结构评分(强度−风险)
    return df


def _eval_one_date(panel: pd.DataFrame, cols: list[str], i: int) -> dict | None:
    """单个采样日：按评分分强/中/弱三档，统计各档 T+5 实际表现。"""
    df = _price_score(panel, cols, i)
    if len(df) < _MIN_VALID:
        return None
    buy, sell = panel[cols[i + 1]], panel[cols[i + 1 + HORIZON]]   # 次日买 / T+5 卖
    df["fwd"] = (sell / buy - 1) * 100
    df = df[df["fwd"].notna()]
    if len(df) < _MIN_VALID:
        return None
    q1, q2 = df["score"].quantile([1 / 3, 2 / 3])
    tiers = {
        "强": _stats(df.loc[df["score"] >= q2, "fwd"]),
        "中": _stats(df.loc[(df["score"] >= q1) & (df["score"] < q2), "fwd"]),
        "弱": _stats(df.loc[df["score"] < q1, "fwd"]),
    }
    spread = (tiers["强"]["win_rate"] - tiers["弱"]["win_rate"]
              if tiers["强"]["win_rate"] is not None and tiers["弱"]["win_rate"] is not None else None)
    return {"run_date": cols[i], "source": "backtest", "tiers": tiers, "spread": spread}


def run_historical(end_date: str = "", lookback: int = 145, step: int = 3,
                   provider=None) -> list[dict]:
    """对过去采样交易日做价格结构评分回测。返回每日结果列表（升序）。"""
    from app.factors.breadth_qfq import build_qfq_panel
    from app.data.composite_provider import CompositeProvider
    provider = provider or CompositeProvider()
    import datetime
    end = (end_date or "").replace("-", "") or datetime.date.today().strftime("%Y%m%d")
    panel = build_qfq_panel(end, provider, lookback=lookback)
    if panel is None or panel.empty or panel.shape[1] < _WARMUP + HORIZON + 3:
        logger.warning("[评分回测] 面板不足，跳过")
        return []
    cols = list(panel.columns)
    out = []
    for i in range(_WARMUP, len(cols) - HORIZON - 1, step):
        r = _eval_one_date(panel, cols, i)
        if r:
            out.append(r)
    return out


# ──────────────────────────────────────────────
# B 前向验证（真实选股池·完整评分）
# ──────────────────────────────────────────────

def eval_pool_date(rows: list[dict]) -> dict | None:
    """单个真实选股池日：按重点分高/低 + ⭐ 聚合 T+5 实际表现（需 t5_return 已到期）。"""
    df = pd.DataFrame([{"focus_score": r.get("focus_score"), "star": r.get("star"),
                        "t5": r.get("t5_return"), "t3": r.get("t3_return")} for r in rows])
    df = df[pd.to_numeric(df["t5"], errors="coerce").notna()]
    if df.empty:
        return None
    df["t5"] = pd.to_numeric(df["t5"], errors="coerce")
    hi = df["focus_score"] >= 75
    tiers = {
        "⭐重点": _stats(df.loc[df["star"] == 1, "t5"]),
        "高分(≥75)": _stats(df.loc[hi, "t5"]),
        "其余(<75)": _stats(df.loc[~hi, "t5"]),
    }
    spread = (tiers["高分(≥75)"]["win_rate"] - tiers["其余(<75)"]["win_rate"]
              if tiers["高分(≥75)"]["win_rate"] is not None and tiers["其余(<75)"]["win_rate"] is not None
              else None)
    return {"source": "forward", "tiers": tiers, "spread": spread}


# ──────────────────────────────────────────────
# 聚合总览
# ──────────────────────────────────────────────

def aggregate(evals: list[dict], strong_key: str, weak_key: str) -> dict:
    """跨日聚合：强档/弱档平均胜率 + 强>弱的天数占比（验证评分单调性）。"""
    sw = [(e["tiers"][strong_key]["win_rate"], e["tiers"][weak_key]["win_rate"])
          for e in evals
          if e["tiers"].get(strong_key, {}).get("win_rate") is not None
          and e["tiers"].get(weak_key, {}).get("win_rate") is not None]
    if not sw:
        return {"n_days": 0}
    s_avg = sum(s for s, _ in sw) / len(sw)
    w_avg = sum(w for _, w in sw) / len(sw)
    beat = sum(1 for s, w in sw if s > w) / len(sw) * 100
    return {"n_days": len(sw), "strong_win": round(s_avg, 1), "weak_win": round(w_avg, 1),
            "spread": round(s_avg - w_avg, 1), "beat_ratio": round(beat, 1)}
