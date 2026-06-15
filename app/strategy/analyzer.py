"""
策略因子分析引擎。

从 strategy.db 读取选股记录 + 实际表现，输出：
  1. 总体统计（T+1/T+3/T+5 胜率、均收益、盈亏比、止损触发率）
  2. 因子分桶分析（RPS50 / RSI_14 / pullback_score / 资金流 分桶后各自胜率）
  3. 市场状态分析（强势/震荡/弱势 下各自表现）
  4. 行业胜率排名（被选最多的行业、各行业 T+1 胜率）
  5. 因子相关性（与 T+1 收益的 Spearman 相关系数）

所有分析函数统一返回 dict，方便 Web UI 和 CLI 双向使用。
"""

import logging
from typing import Any

import numpy as np
import pandas as pd

from app.strategy.db import HORIZONS, get_all_with_performance, get_summary_stats

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 公共入口：一次性计算所有分析维度
# ──────────────────────────────────────────────

def full_analysis(
    is_backtest: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_samples: int = 5,
) -> dict[str, Any]:
    """
    汇总所有分析维度，返回结构化 dict。

    Args:
        is_backtest:  None=全部 / 0=前向实盘 / 1=历史回测
        start_date:   筛选起始日（含）
        end_date:     筛选结束日（含）
        min_samples:  最少样本数才纳入分桶统计（避免小样本误导）

    Returns:
        {
          "overview":      总体统计,
          "by_factor":     因子分桶分析,
          "by_market":     市场状态分析,
          "by_theme":      行业/主题胜率,
          "correlations":  因子相关性,
          "records":       原始记录（用于明细展示）,
          "meta":          数据集信息
        }
    """
    records = get_all_with_performance(
        is_backtest=is_backtest,
        start_date=start_date,
        end_date=end_date,
    )

    if not records:
        return {"error": "暂无数据", "records": []}

    df = pd.DataFrame(records)
    # 只保留有 T+1 收益的记录做主要分析
    df_with_t1 = df[df["t1_return"].notna()].copy()

    meta = {
        "total_records": len(df),
        "with_t1": len(df_with_t1),
        "date_range": f"{df['run_date'].min()} ~ {df['run_date'].max()}",
        "is_backtest": is_backtest,
    }

    return {
        "overview": _calc_overview(df, min_samples),
        "by_factor": _calc_factor_buckets(df_with_t1, min_samples),
        "by_market": _calc_by_market(df_with_t1, min_samples),
        "by_theme": _calc_by_theme(df_with_t1, min_samples),
        "correlations": _calc_correlations(df_with_t1),
        "records": records[:200],  # 最多返回 200 条明细
        "meta": meta,
    }


# ──────────────────────────────────────────────
# 1. 总体统计
# ──────────────────────────────────────────────

def _calc_overview(df: pd.DataFrame, min_samples: int) -> dict:
    result = {}
    for h in HORIZONS:
        col_ret = f"t{h}_return"
        col_win = f"t{h}_win"
        col_stop = f"t{h}_stop" if h == 1 else None

        sub = df[df[col_ret].notna()].copy()
        n = len(sub)
        if n == 0:
            result[f"t{h}"] = {"total": 0}
            continue

        wins = sub[sub[col_win] == 1]
        losses = sub[sub[col_win] == 0]
        avg_win = float(wins[col_ret].mean()) if len(wins) > 0 else 0.0
        avg_loss = float(losses[col_ret].mean()) if len(losses) > 0 else 0.0

        stat = {
            "total": n,
            "win_count": int(len(wins)),
            "win_rate": round(len(wins) / n, 4),
            "avg_return": round(float(sub[col_ret].mean()), 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "profit_loss_ratio": round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else None,
            "max_loss": round(float(sub[col_ret].min()), 4),
            "max_gain": round(float(sub[col_ret].max()), 4),
            "std": round(float(sub[col_ret].std()), 4),
        }
        if col_stop and col_stop in sub.columns:
            stat["stop_rate"] = round(float(sub[col_stop].mean()), 4)

        result[f"t{h}"] = stat

    return result


# ──────────────────────────────────────────────
# 2. 因子分桶分析
# ──────────────────────────────────────────────

_FACTOR_CONFIGS = [
    # (列名, 显示名, 桶数, 反转)
    ("rps50",          "RPS50强度",      4, False),
    ("rsi_14",         "RSI_14动量",     4, False),
    ("pullback_score", "缩量回踩质量",   4, False),
    ("main_net_flow",  "主力净流入(万)", 4, False),
    ("change_pct_7d",  "7日涨幅(%)",     4, True),   # 反转：涨幅越小越好
    ("vwap_deviation", "VWAP偏离(%)",    4, True),   # 反转：偏离越小越好
]


def _calc_factor_buckets(df: pd.DataFrame, min_samples: int) -> list[dict]:
    """对每个因子做四分位分桶，输出各桶的 T+1 胜率。"""
    results = []
    if df.empty or "t1_return" not in df.columns:
        return results

    for col, label, n_bins, reverse in _FACTOR_CONFIGS:
        if col not in df.columns:
            continue
        sub = df[[col, "t1_return", "t1_win"]].dropna()
        if len(sub) < min_samples * n_bins:
            continue

        try:
            sub["bucket"] = pd.qcut(sub[col], q=n_bins, duplicates="drop")
        except Exception:
            continue

        buckets = []
        for bucket, grp in sub.groupby("bucket", observed=True):
            n = len(grp)
            if n < min_samples:
                continue
            buckets.append({
                "label": _fmt_bucket(bucket, reverse),
                "n": n,
                "win_rate": round(float(grp["t1_win"].mean()), 4),
                "avg_return": round(float(grp["t1_return"].mean()), 4),
            })

        if buckets:
            results.append({
                "factor": label,
                "col": col,
                "buckets": buckets,
                # 胜率最高的桶（结论）
                "best_bucket": max(buckets, key=lambda x: x["win_rate"])["label"],
                "best_win_rate": max(b["win_rate"] for b in buckets),
            })

    return results


def _fmt_bucket(bucket, reverse: bool) -> str:
    """将 pandas Interval 格式化为可读字符串。"""
    try:
        lo = round(float(bucket.left), 1)
        hi = round(float(bucket.right), 1)
        return f"{lo} ~ {hi}"
    except Exception:
        return str(bucket)


# ──────────────────────────────────────────────
# 3. 市场状态分析
# ──────────────────────────────────────────────

def _calc_by_market(df: pd.DataFrame, min_samples: int) -> list[dict]:
    """按市场状态（强势/震荡/弱势）分组，输出各自 T+1 胜率。"""
    if "market_label" not in df.columns or df.empty:
        return []

    results = []
    for label, grp in df.groupby("market_label"):
        n = len(grp)
        if n < min_samples:
            continue
        results.append({
            "market": label,
            "n": n,
            "win_rate": round(float(grp["t1_win"].mean()), 4),
            "avg_return": round(float(grp["t1_return"].mean()), 4),
        })
    return sorted(results, key=lambda x: x["win_rate"], reverse=True)


# ──────────────────────────────────────────────
# 4. 行业/主题胜率
# ──────────────────────────────────────────────

def _calc_by_theme(df: pd.DataFrame, min_samples: int) -> list[dict]:
    """按主题/行业分组，找出哪些行业被选中后胜率最高。"""
    if "theme" not in df.columns or df.empty:
        return []

    results = []
    for theme, grp in df.groupby("theme"):
        n = len(grp)
        if n < min_samples:
            continue
        results.append({
            "theme": theme,
            "n": n,
            "win_rate": round(float(grp["t1_win"].mean()), 4),
            "avg_return": round(float(grp["t1_return"].mean()), 4),
        })
    return sorted(results, key=lambda x: x["n"], reverse=True)


# ──────────────────────────────────────────────
# 5. 因子相关性（Spearman）
# ──────────────────────────────────────────────

def _calc_correlations(df: pd.DataFrame) -> list[dict]:
    """
    计算各量化因子与 T+1 收益的 Spearman 相关系数。
    相关系数 > 0 → 因子值越高，收益越好；< 0 → 反之。
    """
    if df.empty or "t1_return" not in df.columns:
        return []

    factor_cols = [col for col, *_ in _FACTOR_CONFIGS if col in df.columns]
    factor_labels = {col: label for col, label, *_ in _FACTOR_CONFIGS}

    results = []
    for col in factor_cols:
        sub = df[[col, "t1_return"]].dropna()
        if len(sub) < 10:
            continue
        try:
            # 纯 numpy 实现 Spearman（不依赖 scipy）
            x_rank = sub[col].rank().to_numpy(dtype=float).copy()
            y_rank = sub["t1_return"].rank().to_numpy(dtype=float).copy()
            x_rank -= x_rank.mean()
            y_rank -= y_rank.mean()
            denom = (np.sqrt((x_rank**2).sum()) * np.sqrt((y_rank**2).sum()))
            corr = float(np.dot(x_rank, y_rank) / denom) if denom > 0 else 0.0
            results.append({
                "factor": factor_labels.get(col, col),
                "col": col,
                "spearman": round(corr, 4),
                "abs_corr": round(abs(corr), 4),
                "direction": "正相关" if corr > 0 else "负相关",
                "strength": _corr_strength(abs(corr)),
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: x["abs_corr"], reverse=True)


def _corr_strength(c: float) -> str:
    if c >= 0.3:
        return "强"
    if c >= 0.15:
        return "中"
    if c >= 0.05:
        return "弱"
    return "极弱"


# ──────────────────────────────────────────────
# CLI 打印
# ──────────────────────────────────────────────

def print_analysis(result: dict) -> None:
    """将 full_analysis 结果打印为可读报告（CLI 用）。"""
    if "error" in result:
        print(f"\n⚠️  {result['error']}")
        return

    meta = result.get("meta", {})
    print(f"\n{'='*60}")
    print(f"  策略验证报告  {'[回测]' if meta.get('is_backtest') == 1 else '[实盘]'}")
    print(f"  数据区间: {meta.get('date_range', '—')}")
    print(f"  总记录: {meta.get('total_records', 0)}  有T+1收益: {meta.get('with_t1', 0)}")
    print(f"{'='*60}")

    # 总体统计
    print("\n▶ 总体胜率 vs 吴川基准 52.11%\n")
    ov = result.get("overview", {})
    print(f"  {'持仓':>5}  {'总笔':>6}  {'胜率':>7}  {'均收益':>8}  {'盈亏比':>7}  {'止损率':>7}")
    print(f"  {'-'*50}")
    for h in HORIZONS:
        s = ov.get(f"t{h}", {})
        if not s.get("total"):
            continue
        wr = f"{s['win_rate']:.1%}"
        flag = "✅" if s["win_rate"] >= 0.5211 else "❌"
        avg = f"{s['avg_return']:+.2f}%"
        plr = f"{s['profit_loss_ratio']:.2f}" if s.get("profit_loss_ratio") else "—"
        sr = f"{s['stop_rate']:.1%}" if s.get("stop_rate") is not None else "—"
        print(f"  T+{h}  {s['total']:>6}  {flag}{wr:>6}  {avg:>8}  {plr:>7}  {sr:>7}")

    # 因子相关性
    corrs = result.get("correlations", [])
    if corrs:
        print(f"\n▶ 因子 → T+1收益 相关性（Spearman）\n")
        for c in corrs:
            bar = "█" * int(c["abs_corr"] * 20)
            print(f"  {c['factor']:<16} {c['spearman']:>+6.3f}  {c['direction']}({c['strength']})  {bar}")

    # 因子分桶
    buckets = result.get("by_factor", [])
    if buckets:
        print(f"\n▶ 因子分桶 T+1 胜率\n")
        for fb in buckets:
            print(f"  {fb['factor']}  (最优区间: {fb['best_bucket']}  胜率: {fb['best_win_rate']:.1%})")
            for b in fb["buckets"]:
                flag = "✅" if b["win_rate"] >= 0.5211 else "  "
                bar = "█" * int(b["win_rate"] * 20)
                print(f"    [{b['label']:>16}]  {flag}{b['win_rate']:.1%}  均收益{b['avg_return']:+.2f}%  n={b['n']}  {bar}")

    # 市场状态
    mkt = result.get("by_market", [])
    if mkt:
        print(f"\n▶ 市场状态 T+1 胜率\n")
        for m in mkt:
            flag = "✅" if m["win_rate"] >= 0.5211 else "❌"
            print(f"  {m['market']:<6}  {flag}{m['win_rate']:.1%}  均收益{m['avg_return']:+.2f}%  n={m['n']}")

    # 行业排名
    themes = result.get("by_theme", [])
    if themes:
        print(f"\n▶ 行业/主题 T+1 胜率（Top10 按样本量）\n")
        for t in themes[:10]:
            flag = "✅" if t["win_rate"] >= 0.5211 else "❌"
            print(f"  {t['theme']:<12}  {flag}{t['win_rate']:.1%}  均收益{t['avg_return']:+.2f}%  n={t['n']}")

    print(f"\n{'='*60}\n")
