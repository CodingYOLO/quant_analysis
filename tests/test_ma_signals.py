"""
短线均线信号 ma_signals 单测（合成数据精确构造，零网络）。

可直接运行：python -m tests.test_ma_signals
"""

from __future__ import annotations

import pandas as pd

import app.factors.patterns  # noqa: F401  触发注册
from app.factors.patterns import ma_signals as M
from app.factors.patterns.base import PATTERN_REGISTRY


def _mk(closes, vols=None, lows=None, opens=None) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "open": list(opens) if opens else list(closes),
        "high": list(closes),
        "low": list(lows) if lows else list(closes),
        "close": list(closes),
        "vol": list(vols) if vols else [1000] * n,
        "amount": [1e5] * n, "pct_chg": [0.0] * n,
    })


def test_all_signals_registered() -> None:
    for key in ("ma5_cross_ma10", "shrink_pullback_ma10", "ma_short_bull",
                "first_above_ma10", "macd_gold_above_zero", "rsi_oversold_recover",
                "big_yang_volume"):
        assert key in PATTERN_REGISTRY, f"信号 {key} 未注册"


def test_ma5_cross_ma10() -> None:
    assert M.MA5CrossMA10().detect(_mk([10] * 11 + [20])) is True     # 末根放量拉升 → MA5上穿MA10
    assert M.MA5CrossMA10().detect(_mk([10] * 12)) is False           # 全平 → 不交叉


def test_first_above_ma10() -> None:
    assert M.FirstAboveMA10().detect(_mk([10] * 10 + [8, 14])) is True  # 昨破位、今首次收上MA10
    assert M.FirstAboveMA10().detect(_mk([10] * 12)) is False


def test_ma_short_bull() -> None:
    assert M.MAShortBull().detect(_mk([10 + 0.5 * i for i in range(22)])) is True   # 持续上行→短多头
    assert M.MAShortBull().detect(_mk([20 - 0.5 * i for i in range(22)])) is False  # 持续下行


def test_shrink_pullback_ma10() -> None:
    closes = [10 + 0.3 * i for i in range(16)]                # 上行 → MA10 上行、收在 MA10 上
    lows = list(closes); lows[-1] = closes[-1] * 0.92         # 末根盘中回踩到 MA10 附近
    vols = [1000] * 15 + [400]                                # 末根缩量
    assert M.ShrinkPullbackMA10().detect(_mk(closes, vols=vols, lows=lows)) is True
    # 不缩量 → 不命中
    assert M.ShrinkPullbackMA10().detect(_mk(closes, vols=[1000] * 15 + [3000], lows=lows)) is False
    # 没回踩到 MA10（低点=收盘，高高在上）→ 不命中
    assert M.ShrinkPullbackMA10().detect(_mk(closes, vols=vols)) is False


def test_big_yang_volume() -> None:
    closes = [10] * 6 + [10.8]                                # 末根 +8%
    opens = [10] * 6 + [10.0]                                 # 收阳（收>开）
    vols = [1000] * 6 + [2000]                                # 放量
    assert M.BigYangVolume().detect(_mk(closes, vols=vols, opens=opens)) is True
    assert M.BigYangVolume().detect(_mk([10] * 6 + [10.3], vols=vols, opens=opens)) is False  # 仅+3%


def test_macd_rsi_flat_false_and_no_crash() -> None:
    flat = _mk([10] * 40)
    assert M.MacdGoldAboveZero().detect(flat) is False        # 全平无金叉
    assert M.RsiOversoldRecover().detect(flat) is False       # 全平不构成超卖回升
    # 真实形态序列只要不抛异常即可（正样本由真实数据回测验证）
    rising = _mk([10 + 0.4 * i for i in range(40)])
    assert isinstance(M.MacdGoldAboveZero().detect(rising), bool)
    assert isinstance(M.RsiOversoldRecover().detect(rising), bool)


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
