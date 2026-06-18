"""
新增技术因子单测：KDJ金叉 / EMA多头 / TD神奇九转 / 影线。

零依赖，可直接运行：python -m tests.test_core_new_factors
"""

from __future__ import annotations

import pandas as pd

from app.factors import core as F


def test_ema_bull() -> None:
    rising = pd.Series([10 + i * 0.1 for i in range(40)])
    assert F.ema_bull(rising) is True
    falling = pd.Series([20 - i * 0.1 for i in range(40)])
    assert F.ema_bull(falling) is False


def test_kdj_golden_cross_low() -> None:
    # 先跌(KDJ 低位) 后今日反弹 → 低位金叉
    closes = [20 - i * 0.4 for i in range(15)] + [12.5, 13.2]
    s = pd.Series(closes)
    hi = s + 0.3
    lo = s - 0.3
    assert F.kdj_golden_cross(s, hi, lo) is True
    # 持续上涨高位 → 不应触发低位金叉
    up = pd.Series([10 + i * 0.3 for i in range(20)])
    assert F.kdj_golden_cross(up, up + 0.2, up - 0.2) is False


def test_td_buy_setup_count() -> None:
    # 连续 9 根 close < 前4根 → 计数≥9
    base = [30, 30, 30, 30]                       # 前置4根（比较基准）
    seq = base + [29 - i for i in range(10)]      # 连续走低
    assert F.td_buy_setup_count(pd.Series(seq)) >= 9
    # 上涨序列 → 计数 0
    assert F.td_buy_setup_count(pd.Series([10 + i for i in range(15)])) == 0


def test_shadow_ratio() -> None:
    # 长下影：open=close=10, high=10.1, low=9 → 下影占全幅 ≈0.9（≥0.5）
    up_r, dn_r = F.shadow_ratio(10.0, 10.1, 9.0, 10.0)
    assert dn_r >= 0.5 and dn_r > up_r
    # 长上影：open=close=10, high=11, low=9.9 → 上影占全幅 ≈0.9
    up_r2, dn_r2 = F.shadow_ratio(10.0, 11.0, 9.9, 10.0)
    assert up_r2 >= 0.5 and up_r2 > dn_r2
    # 退化（high==low）→ 0,0 不报错
    assert F.shadow_ratio(10.0, 10.0, 10.0, 10.0) == (0.0, 0.0)


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
