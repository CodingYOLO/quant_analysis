"""accum_score 前向收益验证（纯本地分析·不接入网站·不修改任何线上逻辑）。

方法学（力求不自欺）：
  · 无未来函数：在历史日 T 只用 ≤T 的价量算 accum_score（结构口径·剔除8分大单噪音），
    再 measure T→T+10 / T→T+20 的真实收益。
  · 超额收益(alpha)：每只票收益减去当期全市场均值，剔除大盘beta，看的是真本事。
  · 多个 T 日、非重叠前向窗口，按分数分桶看单调性 + 秩相关，避免单日运气。
用法：python scripts/backtest_accum_score.py
"""

from __future__ import annotations

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix
from app.strategy.screener import _accum_factor_columns, _accumulation_score

_END_DATE = "20260622"
_WINDOW = 200            # 载入交易日数（含历史+前向）
_HORIZONS = (10, 20, 40, 60)     # 前向持有交易日（含长周期·吸筹→主升常需数周到数月）
_PRICE_FLOOR = 2.0       # 剔除仙股噪音
_BANDS = [(0, 50), (50, 65), (65, 75), (75, 85), (85, 101)]


def _scores_at(close_m, high_m, low_m, vol_m, ti: int) -> pd.Series:
    """T=ti 处的结构化 accum_score（只用 ≤ti 的数据·大单分用 NaN 占位即剔除）。"""
    sl = slice(0, ti + 1)
    cols = _accum_factor_columns(close_m.iloc[sl], high_m.iloc[sl], low_m.iloc[sl], vol_m.iloc[sl])
    f = pd.DataFrame(cols)
    return f.apply(
        lambda r: _accumulation_score(
            r.get("vol5_vol20"), r.get("ma20_slope"), r.get("ret20"), r.get("amp20"),
            r.get("big_up_days_20"), float("nan"),
            up_down_vol=r.get("up_down_vol"), amp_contract=r.get("amp_contract")),
        axis=1,
    )


def _collect(close_m, high_m, low_m, vol_m) -> dict[int, pd.DataFrame]:
    """在多个非重叠 T 日采样 {horizon: 汇总DataFrame(score, ret, excess)}。"""
    n = len(close_m)
    t_list = [n - 1 - 20 * i for i in range(1, 8)]      # 7 个 T（间隔20日），长周期也够样本
    pools: dict[int, list[pd.DataFrame]] = {k: [] for k in _HORIZONS}
    for ti in t_list:
        score = _scores_at(close_m, high_m, low_m, vol_m, ti)
        c0 = close_m.iloc[ti]
        for k in _HORIZONS:
            if ti + k > n - 1:
                continue
            ret = (close_m.iloc[ti + k] / c0 - 1) * 100
            df = pd.DataFrame({"score": score, "ret": ret, "c0": c0}).dropna()
            df = df[df["c0"] > _PRICE_FLOOR]
            df["excess"] = df["ret"] - df["ret"].mean()   # 剔除当期大盘beta
            pools[k].append(df[["score", "ret", "excess"]])
    return {k: pd.concat(v) for k, v in pools.items() if v}


def _report(k: int, pool: pd.DataFrame) -> None:
    print(f"\n========== T+{k} 日前向收益（样本 {len(pool)} 只·{5} 个交易日采样） ==========")
    base = pool["ret"].mean()
    print(f"全样本均值收益 {base:+.2f}%  | 胜率(>0) {(pool['ret'] > 0).mean()*100:.1f}%")
    print(f"{'吸筹分档':<12}{'样本':>6}{'均收益%':>9}{'超额%':>9}{'胜率%':>8}{'跑赢大盘%':>10}")
    for lo, hi in _BANDS:
        g = pool[(pool["score"] >= lo) & (pool["score"] < hi)]
        if g.empty:
            continue
        print(f"{f'[{lo},{hi})':<12}{len(g):>6}{g['ret'].mean():>9.2f}{g['excess'].mean():>9.2f}"
              f"{(g['ret']>0).mean()*100:>8.1f}{(g['excess']>0).mean()*100:>10.1f}")
    # 头尾十分位 + 秩相关
    q = pool["score"].rank(pct=True)
    top, bot = pool[q >= 0.9], pool[q <= 0.1]
    print(f"  顶10%吸筹分: 均收益 {top['ret'].mean():+.2f}% / 超额 {top['excess'].mean():+.2f}% (n={len(top)})")
    print(f"  底10%吸筹分: 均收益 {bot['ret'].mean():+.2f}% / 超额 {bot['excess'].mean():+.2f}% (n={len(bot)})")
    # Spearman = 秩的 Pearson（手算·免 scipy 依赖）
    rho = pool["score"].rank().corr(pool["ret"].rank())
    rho_ex = pool["score"].rank().corr(pool["excess"].rank())
    print(f"  秩相关 Spearman(吸筹分, 收益)={rho:+.3f} | (吸筹分, 超额)={rho_ex:+.3f}"
          f"   (≈0=没用, >0.1=有点用, >0.3=较强)")


def main() -> None:
    prov = CompositeProvider()
    print(f"载入 {_WINDOW} 日价量矩阵（截至 {_END_DATE}）…")
    close_m, _open_m, high_m, low_m, vol_m = load_price_matrix(_END_DATE, prov, n_days=_WINDOW)
    print(f"矩阵 {close_m.shape[0]} 日 × {close_m.shape[1]} 股 | {close_m.index[0]}→{close_m.index[-1]}")
    pools = _collect(close_m, high_m, low_m, vol_m)
    for k in _HORIZONS:
        if k in pools:
            _report(k, pools[k])
    print("\n说明：超额=个股收益−当期全样本均值(剔除大盘)；若各档超额随吸筹分单调上升、"
          "且顶10%超额>0、秩相关>0，则分数有区分力；否则应推翻重做。")


if __name__ == "__main__":
    main()
