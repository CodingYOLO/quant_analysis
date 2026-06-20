"""
大盘状态判定 market_regime 单测（纯函数，零依赖）。

可直接运行：python -m tests.test_market_regime
"""

from __future__ import annotations

import math

import pandas as pd

import app.backtest.market_regime as MR


def test_classify_cases() -> None:
    assert MR.classify(110, 100, 90) == "强势"      # 站上 MA20 & MA60
    assert MR.classify(80, 100, 90) == "弱势"        # 双破
    assert MR.classify(105, 100, 110) == "震荡"      # MA20 上、MA60 下
    assert MR.classify(95, 100, 90) == "震荡"        # MA20 下、MA60 上
    assert MR.classify(math.nan, 100, 90) == ""      # 数据不足
    # MA60 早期缺失 → 回退用 MA20 同侧
    assert MR.classify(110, 100, math.nan) == "强势"
    assert MR.classify(90, 100, math.nan) == "弱势"


def test_build_regime_map_uptrend_strong() -> None:
    n = 80
    df = pd.DataFrame({"trade_date": [f"d{i:03d}" for i in range(n)],
                       "close": [100 + i for i in range(n)]})
    m = MR.build_regime_map(df)
    assert m[f"d{n-1:03d}"] == "强势"                 # 持续上行末端强势
    assert m["d000"] == ""                            # 头部 MA20 不足 → 未知


def test_build_regime_map_downtrend_weak() -> None:
    n = 80
    df = pd.DataFrame({"trade_date": [f"d{i:03d}" for i in range(n)],
                       "close": [200 - i for i in range(n)]})
    assert MR.build_regime_map(df)[f"d{n-1:03d}"] == "弱势"


def test_build_regime_map_empty() -> None:
    assert MR.build_regime_map(pd.DataFrame()) == {}
    assert MR.build_regime_map(None) == {}


def test_occupancy() -> None:
    m = {"a": "强势", "b": "强势", "c": "弱势", "d": ""}
    occ = MR.occupancy(m, ["a", "b", "c", "d"])      # 有效3个：强2 弱1
    assert occ["强势"] == 66.7 and occ["弱势"] == 33.3 and occ["震荡"] == 0.0
    assert MR.occupancy(m, []) == {"强势": 0.0, "震荡": 0.0, "弱势": 0.0}


def test_index_label() -> None:
    assert MR.index_label("000300.SH") == "沪深300"
    assert MR.index_label("999999.XX") == "999999.XX"   # 未知回显代码


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
