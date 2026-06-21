"""
自选股今日信号 signal_watch 纯函数单测：卖点规则（今日首破MA20 / 乖离过热）。

买点(_best_firing_buy)依赖真实K线+scout，由真实数据验证覆盖，此处只测确定性的卖点规则。
零依赖：python -m tests.test_signal_watch
"""

from __future__ import annotations

import pandas as pd

import app.strategy.signal_watch as SW


def _k(close_list) -> pd.DataFrame:
    return pd.DataFrame({"close": pd.Series([float(x) for x in close_list])})


def test_sell_break_ma20() -> None:
    # 前一日在MA20上、今日跌破 → 破位
    out = SW._sell_signals(_k([10.0] * 23 + [10.5, 9.0]))
    assert any(s["kind"] == "破位" for s in out)


def test_sell_overheat() -> None:
    # 大幅高于MA20(乖离≥20%) → 过热止盈警示（且未破位）
    out = SW._sell_signals(_k([10.0] * 24 + [13.0]))
    assert any(s["kind"] == "过热" for s in out)
    assert not any(s["kind"] == "破位" for s in out)


def test_sell_none_when_healthy() -> None:
    # 平稳站上MA20·不过热 → 无卖点
    assert SW._sell_signals(_k([10.0] * 24 + [10.3])) == []


def test_sell_short_series() -> None:
    assert SW._sell_signals(_k([10.0] * 10)) == []


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
