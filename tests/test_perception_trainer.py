"""盘感训练核心——零未来泄漏 + 分档/评分 单测（纯函数·不连网）。"""

from __future__ import annotations

import pandas as pd

from app.strategy.perception_trainer import (bucket_of, classify_setup,
                                             position_metrics, score, split_at)


def _kline(closes: list[float]) -> pd.DataFrame:
    """合成日线：用收盘序列造 OHLCV（开=前收·高低±1%·量恒定）。"""
    rows = []
    for k, c in enumerate(closes):
        prev = closes[k - 1] if k else c
        rows.append({"trade_date": f"2026{1000 + k}", "open": prev,
                     "high": max(prev, c) * 1.01, "low": min(prev, c) * 0.99,
                     "close": c, "vol": 1e6,
                     "pct_chg": round((c / prev - 1) * 100, 2) if prev else 0.0})
    return pd.DataFrame(rows)


def test_split_no_overlap_and_t0() -> None:
    kl = _kline([10 + i for i in range(20)])
    hist, future = split_at(kl, 9)
    assert len(hist) == 10 and len(future) == 10
    assert float(hist["close"].iloc[-1]) == float(kl["close"].iloc[9])   # hist 末=T0
    assert float(future["close"].iloc[0]) == float(kl["close"].iloc[10]) # future 始=T0+1
    # 无重叠：拼回去 == 原序列
    assert hist["close"].tolist() + future["close"].tolist() == kl["close"].tolist()


def test_zero_future_leakage() -> None:
    """命根子：题面指标只用 hist——把 future 改成乱七八糟，hist 侧指标必须纹丝不动。"""
    base = [10 + i * 0.1 for i in range(120)]
    kl1 = _kline(base)
    kl2 = _kline(base[:80] + [999.0] * 40)          # 仅篡改 T0(=79) 之后的 future
    h1, _ = split_at(kl1, 79)
    h2, _ = split_at(kl2, 79)
    assert position_metrics(h1, None) == position_metrics(h2, None)      # 位置指标不受未来影响
    assert classify_setup(h1) == classify_setup(h2)                      # 形态归类亦然


def test_bucket_boundaries() -> None:
    assert bucket_of(12) == "big_up"
    assert bucket_of(8) == "big_up"          # 8 含在大涨
    assert bucket_of(5) == "up"
    assert bucket_of(0) == "flat"
    assert bucket_of(-2) == "flat"           # 边界 [lo,hi)：-2 归震荡(-2~2)
    assert bucket_of(-3) == "down"           # 小跌区[-8~-2)
    assert bucket_of(-8) == "down"           # 边界 lo 含：-8 仍归小跌(大跌取 <-8)
    assert bucket_of(-20) == "big_down"


def test_score_exact_near_opposite() -> None:
    assert score("up", "up")["points"] == 1.0 and score("up", "up")["exact"]
    assert score("up", "big_up")["points"] == 0.5 and score("up", "big_up")["near"]
    assert score("big_up", "big_down")["points"] == 0.0
    assert score("up", "big_up")["direction_right"] is True       # 都是涨方向
    assert score("up", "down")["direction_right"] is False


def test_classify_setup_basic() -> None:
    assert classify_setup(_kline([10] * 19 + [11.0]))[0] == "limit_up"        # 末根+10%
    assert classify_setup(_kline([10 - i * 0.3 for i in range(30)]))[0] in ("weak", "oversold")


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_perception_trainer 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
