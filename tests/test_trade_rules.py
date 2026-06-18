"""
交易规则文本 trade_rules 单元测试（多条件止损 + 次日验证清单）。

零依赖，可直接运行：python -m tests.test_trade_rules
"""

from __future__ import annotations

from app.factors.trade_rules import (
    build_stop_rule, build_nextday_checklist, _OPEN_FLOOR_PCT,
)


def test_stop_rule_contains_three_levels_and_theme() -> None:
    txt = build_stop_rule(352.95, theme="半导体", ma20=352.95)
    assert "352.95" in txt                 # 止损位价
    assert "放量下跌" in txt                # 量价条件
    assert "净流出" in txt                  # 资金条件
    assert "半导体" in txt                  # 题材级
    assert "20日均线" in txt                # ma20 附注


def test_stop_rule_without_ma20() -> None:
    txt = build_stop_rule(10.50, theme="光伏")
    assert "10.50" in txt and "光伏" in txt
    assert "20日均线" not in txt            # 未给 ma20 则不附注


def test_stop_rule_zero_price_fallback() -> None:
    txt = build_stop_rule(0.0)
    assert "止损位" in txt and "所属板块" in txt


def test_nextday_checklist_quantified() -> None:
    rules = build_nextday_checklist(100.0)
    assert len(rules) == 4
    # 开盘下限 = 100 × (1-2%) = 98.0
    floor = round(100.0 * (1 - _OPEN_FLOOR_PCT / 100), 2)
    assert str(floor) in rules[0]
    assert "量能" in rules[1] and "净流入" in rules[2] and "低开低走" in rules[3]


def test_nextday_checklist_zero_close() -> None:
    rules = build_nextday_checklist(0.0)
    assert len(rules) == 4 and "昨收" in rules[0]


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
