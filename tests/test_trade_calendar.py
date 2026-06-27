"""交易日历判定单测（注入开市日集合·零网络）。"""

from __future__ import annotations

from app.strategy import trade_calendar as tc


def _set_opens(opens: set[str]) -> None:
    """直接注入缓存，绕过 Tushare（模拟一周：周一~五开市，六日休）。"""
    tc._CACHE["key"] = tc._today()
    tc._CACHE["open"] = opens


# 2026-06-29(一)~07-03(五) 开市，07-04(六)/05(日) 休
_WEEK = {"20260629", "20260630", "20260701", "20260702", "20260703"}


def test_is_trading_day() -> None:
    _set_opens(_WEEK)
    assert tc.is_trading_day("20260703") is True            # 周五·开市
    assert tc.is_trading_day("20260704") is False           # 周六·休
    assert tc.is_trading_day("20260705") is False           # 周日·休


def test_next_and_last_trading_day() -> None:
    _set_opens(_WEEK)
    assert tc.next_trading_day("20260630") == "20260701"    # 周二 → 周三
    assert tc.last_trading_day("20260705") == "20260703"    # 周日 → 上个周五
    assert tc.last_trading_day("20260702") == "20260702"    # 当日即交易日


def test_is_last_nontrading_before_open() -> None:
    """周日(05)休、下周一(06)开 → 周日是重开前最后一晚；节假日最后一晚同理。"""
    _set_opens({"20260703", "20260706"})                    # 周五开、下周一开，中间六日休
    assert tc.is_last_nontrading_before_open("20260705") is True    # 周日休·次日(06)开 → True
    assert tc.is_last_nontrading_before_open("20260704") is False   # 周六休·次日(05)仍休 → False
    assert tc.is_last_nontrading_before_open("20260703") is False   # 本身交易日 → False


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_trade_calendar 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
