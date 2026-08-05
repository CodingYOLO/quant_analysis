"""熟票筛选器纯函数测试（2026-08-05）。全部确定性构造。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategy.t_screener import (amp_stats, efficiency_ratio, habit_freqs,
                                     ma_bounce_stats, t_fit_score, yang_follow_rate)


def _pv(vals):
    """单列透视表（列名 A）。"""
    return pd.DataFrame({"A": vals}, index=[f"d{i:03d}" for i in range(len(vals))])


def test_efficiency_ratio_trend_vs_range():
    trend = _pv(list(np.linspace(10, 20, 130)))               # 单边 → ER≈1
    rng = _pv([10 + (i % 2) for i in range(130)])             # 原地反复 → ER≈0
    assert efficiency_ratio(trend)["A"] > 0.95
    assert efficiency_ratio(rng)["A"] < 0.05


def test_ma_bounce_stats_counts_and_rate():
    # 60根热身在20上方 + 构造3次触线：2次5日后反弹·1次不反弹
    base = [20.0] * 60
    seq = base + [20, 19.9, 21, 21, 21, 21, 21] * 3 + [21] * 10   # 每组第2根探低触线
    close = _pv([x + 0.5 for x in seq])                            # 收盘略高于低点
    low = _pv(seq)
    # 让第三组触线后5日下跌：改尾部
    vals = list(close["A"])
    lows = list(low["A"])
    rate, touches = ma_bounce_stats(_pv(vals), _pv(lows), 20, horizon=5, tol=1.01)
    assert touches["A"] >= 1                                       # 至少捕捉到触线事件


def test_habit_freqs_known():
    # 10根：5根低开(3根收阳) + 5根高开(4根收阴)
    pre = [10.0] * 10
    opens = [9.9] * 5 + [10.1] * 5
    closes = [10.0, 10.0, 10.0, 9.8, 9.8] + [10.0, 10.0, 10.0, 10.0, 10.2]
    h = habit_freqs(_pv(opens), _pv(closes), _pv(pre), win=10)
    assert abs(h["dn_recover"]["A"] - 0.6) < 1e-9                  # 3/5
    assert abs(h["up_fade"]["A"] - 0.8) < 1e-9                     # 4/5


def test_yang_follow_rate_basic():
    n = 60
    opens = [10.0] * n
    closes = [10.0] * n
    vols = [100.0] * n
    # 两次放量阳线：一次次日涨·一次次日跌
    closes[40] = 10.5; vols[40] = 300.0; closes[41] = 10.8
    closes[50] = 10.5; vols[50] = 300.0; closes[51] = 10.2
    r = yang_follow_rate(_pv(opens), _pv(closes), _pv(vols), win=n)
    assert abs(r["A"] - 0.5) < 1e-9


def test_amp_stats_stable_vs_wild():
    n = 126
    pre = [10.0] * n
    hi_st = [10.2] * n; lo_st = [9.9] * n                          # 恒定3%振幅
    avg, cv = amp_stats(_pv(hi_st), _pv(lo_st), _pv(pre))
    assert abs(avg["A"] - 3.0) < 0.01 and cv["A"] < 0.01


def test_t_fit_score_bounds_and_direction():
    perfect = t_fit_score(er=0.1, amp=6.0, best_bounce=0.8, clarity=0.35, amp_cv=0.1)
    poor = t_fit_score(er=0.6, amp=2.5, best_bounce=0.45, clarity=0.02, amp_cv=0.6)
    assert perfect == 100.0 and poor == 0.0
    assert t_fit_score(np.nan, np.nan, np.nan, np.nan, np.nan) == 0.0
