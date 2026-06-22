"""慢牛吸筹因子单测：吸筹评分(纯函数) + 多日因子矩阵计算（零网络·合成数据）。"""

from __future__ import annotations

import math

import pandas as pd

import app.strategy.screener as SC


# ---------------------------------------------------------------------------
# 1. 吸筹评分 _accumulation_score（甜区给分·NaN兜底·夹值）
# ---------------------------------------------------------------------------

def test_score_ideal_full_marks() -> None:
    """温和放量+缓升+低波+隐蔽+主力进 → 满分附近。"""
    s = SC._accumulation_score(vol_ratio=1.6, ma20_slope=6.0, ret20=12.0,
                               amp20=3.0, big_up_days=0, main_net_3d=1.0)
    assert s == 100.0


def test_score_explosive_volume_loses_vol_points() -> None:
    """爆量(量比5)→ 量能项不得分，其余满 → 75。"""
    s = SC._accumulation_score(vol_ratio=5.0, ma20_slope=6.0, ret20=12.0,
                               amp20=3.0, big_up_days=0, main_net_3d=1.0)
    assert s == 75.0


def test_score_not_hidden_when_big_up_days() -> None:
    """近20日大涨3天 → 隐蔽项归零（10-3*4<0），低于全隐蔽。"""
    hidden = SC._accumulation_score(1.6, 6.0, 12.0, 3.0, big_up_days=0, main_net_3d=1.0)
    exposed = SC._accumulation_score(1.6, 6.0, 12.0, 3.0, big_up_days=3, main_net_3d=1.0)
    assert exposed == hidden - 10  # 隐蔽项满分10被吃掉


def test_score_fund_outflow_no_fund_points() -> None:
    s_in = SC._accumulation_score(1.6, 6.0, 12.0, 3.0, 0, main_net_3d=1.0)
    s_out = SC._accumulation_score(1.6, 6.0, 12.0, 3.0, 0, main_net_3d=-1.0)
    assert s_out == s_in - 10


def test_score_nan_safe_zero() -> None:
    """全 NaN/None 不抛错，分数为 0。"""
    nan = float("nan")
    s = SC._accumulation_score(nan, nan, nan, nan, nan, nan)
    assert s == 0.0


def test_score_clamped_to_100() -> None:
    s = SC._accumulation_score(1.6, 5.0, 12.0, 1.0, 0, 99.0)
    assert 0.0 <= s <= 100.0


# ---------------------------------------------------------------------------
# 2. 多日因子矩阵 _accum_factor_columns（合成 30 日矩阵·向量化）
# ---------------------------------------------------------------------------

def _slow_bull_matrix():
    """构造一只"温和放量·缓慢走高"的 30 日票 A。"""
    close = pd.DataFrame({"A": [10 + 0.05 * i for i in range(30)]})   # 每日小步上涨
    high = close + 0.1
    low = close - 0.1
    vol = pd.DataFrame({"A": [100] * 25 + [160] * 5})                # 最近5日温和放量
    return close, high, low, vol


def test_accum_columns_slow_bull() -> None:
    close, high, low, vol = _slow_bull_matrix()
    cols = SC._accum_factor_columns(close, high, low, vol)
    # 温和放量比 ≈ 160 / 115 ≈ 1.39，落在甜区
    assert math.isclose(cols["vol5_vol20"]["A"], 160 / 115, rel_tol=1e-3)
    assert 1.2 <= cols["vol5_vol20"]["A"] <= 2.5
    # 近20日涨幅 ≈ (11.45/10.45-1)*100 ≈ 9.57%
    assert math.isclose(cols["ret20"]["A"], (11.45 / 10.45 - 1) * 100, rel_tol=1e-2)
    # MA20 斜率向上
    assert cols["ma20_slope"]["A"] > 0
    # 缓慢走高·无大涨 → 隐蔽
    assert cols["big_up_days_20"]["A"] == 0
    # 低振幅
    assert cols["amp20"]["A"] < 3.5


def test_accum_columns_counts_big_up_days() -> None:
    """含一个 +10% 跳涨日 → big_up_days_20 计为 1。"""
    seq = [10.0] * 20 + [11.0] + [11.05 + 0.01 * i for i in range(9)]  # 第21日 +10%
    close = pd.DataFrame({"B": seq})
    high, low = close + 0.1, close - 0.1
    vol = pd.DataFrame({"B": [100] * 30})
    cols = SC._accum_factor_columns(close, high, low, vol)
    assert cols["big_up_days_20"]["B"] == 1


def test_accum_columns_short_history_skips() -> None:
    """历史不足(<21日)→ 不产出涨幅/隐蔽列，不报错。"""
    close = pd.DataFrame({"C": [10.0 + i for i in range(10)]})
    cols = SC._accum_factor_columns(close, close + 0.1, close - 0.1,
                                    pd.DataFrame({"C": [100] * 10}))
    assert "ret20" not in cols and "big_up_days_20" not in cols


# ---------------------------------------------------------------------------
# runner（无 pytest 依赖）
# ---------------------------------------------------------------------------

def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_screener_accum 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
