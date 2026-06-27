"""盯盘提醒：触发逻辑 + 交易时段判断单测（纯函数·零网络）。"""

from __future__ import annotations

import datetime

from app.strategy.watch_alert import compute_triggers, is_market_hours


def _keys(row):
    return {k for k, _ in compute_triggers(row)}


def test_at_buy_and_near_buy() -> None:
    # 现价≤目标 → 已到买入区
    assert "at_buy" in _keys({"price": 9.5, "target_price": 10.0})
    # 现价高于目标但 ≤3% → 逼近买入区
    assert "near_buy" in _keys({"price": 10.2, "target_price": 10.0})
    # 高于目标 >3% → 不触发
    assert compute_triggers({"price": 11.0, "target_price": 10.0}) == []


def test_break_stop_and_moves() -> None:
    assert "break_stop" in _keys({"price": 8.9, "stop_loss": 9.0})
    assert "big_drop" in _keys({"price": 10, "pct_chg": -7.5})
    assert "near_limit" in _keys({"price": 10, "pct_chg": 9.9})
    assert compute_triggers({"price": 10, "pct_chg": -2.0}) == []   # 小跌不触发


def test_market_hours() -> None:
    mon = datetime.datetime(2026, 6, 29, 10, 0)      # 周一上午
    assert is_market_hours(mon) is True
    assert is_market_hours(datetime.datetime(2026, 6, 29, 12, 0)) is False   # 午休
    assert is_market_hours(datetime.datetime(2026, 6, 29, 16, 0)) is False   # 盘后
    assert is_market_hours(datetime.datetime(2026, 6, 27, 10, 0)) is False   # 周六


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_watch_alert 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
