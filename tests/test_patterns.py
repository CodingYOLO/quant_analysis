"""
K线/量价形态库单元测试（合成 OHLCV 断言命中/不命中）。

零依赖，可直接运行：python -m tests.test_patterns
"""

from __future__ import annotations

import pandas as pd

from app.factors.patterns.base import PATTERN_REGISTRY, detect_all
from app.factors.patterns.price_volume import (
    BreakoutPriorHigh, ShrinkPullbackMA20, MABullStack, PlatformBreakout, VolPriceSurge,
)


def _ohlcv(closes, vols, opens=None, highs=None, lows=None) -> pd.DataFrame:
    n = len(closes)
    opens = opens or [c * 0.99 for c in closes]
    highs = highs or [max(o, c) * 1.005 for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) * 0.995 for o, c in zip(opens, closes)]
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "vol": vols})


def test_breakout_prior_high_hit_and_miss() -> None:
    p = BreakoutPriorHigh(n=20)
    # 前20日在10附近震荡，今日11(创新高)+放量
    closes = [10 + (i % 3) * 0.1 for i in range(25)] + [11.0]
    vols = [1000] * 25 + [2000]
    assert p.detect(_ohlcv(closes, vols)) is True
    # 今日不放量 → 不命中
    assert p.detect(_ohlcv(closes, [1000] * 26)) is False
    # 今日不创新高 → 不命中
    closes2 = closes[:-1] + [10.2]
    assert p.detect(_ohlcv(closes2, vols)) is False


def test_ma_bull_stack() -> None:
    p = MABullStack()
    rising = [10 + i * 0.1 for i in range(70)]      # 持续上行 → 均线多头
    assert p.detect(_ohlcv(rising, [1000] * 70)) is True
    falling = [20 - i * 0.1 for i in range(70)]     # 持续下行 → 空头
    assert p.detect(_ohlcv(falling, [1000] * 70)) is False


def test_vol_price_surge() -> None:
    p = VolPriceSurge()
    closes = [10] * 9 + [10.5]                       # 今日收涨
    vols = [1000] * 9 + [2000]                       # 放量
    assert p.detect(_ohlcv(closes, vols)) is True
    assert p.detect(_ohlcv([10] * 9 + [9.5], vols)) is False   # 收跌→不命中


def test_platform_breakout() -> None:
    p = PlatformBreakout(n=15)
    box = [10 + (i % 2) * 0.1 for i in range(16)]    # 窄幅箱体
    closes = box + [11.0]                            # 突破上沿
    vols = [1000] * 16 + [1800]                      # 放量
    assert p.detect(_ohlcv(closes, vols)) is True
    # 未突破 → 不命中
    assert p.detect(_ohlcv(box + [10.1], vols)) is False


def test_registry_and_detect_all() -> None:
    assert len(PATTERN_REGISTRY) >= 5
    rising = [10 + i * 0.1 for i in range(70)]
    hits = detect_all(_ohlcv(rising, [1000] * 69 + [2000]))
    assert set(hits.keys()) == set(PATTERN_REGISTRY.keys())
    assert hits["ma_bull_stack"] is True             # 持续上行必多头


def test_min_bars_guard() -> None:
    # 数据不足 min_bars → detect_all 返回 False，不抛异常
    short = _ohlcv([10, 11, 12], [1000, 1000, 2000])
    hits = detect_all(short)
    assert all(v is False for v in hits.values())


def test_break5_recover() -> None:
    """破五反五：近3日内跌破MA5、今日收回MA5之上 → 命中；全程在MA5上方 → 不命中。"""
    p = PATTERN_REGISTRY["break5_recover"]
    dip_recover = _ohlcv([10, 10, 10, 10, 10, 10, 10, 10, 8, 8, 11], [1000] * 11)  # 跌破后今日收回
    assert p.detect(dip_recover) is True
    no_dip = _ohlcv([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20], [1000] * 11)      # 一路在MA5上方
    assert p.detect(no_dip) is False


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
