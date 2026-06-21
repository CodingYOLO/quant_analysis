"""
持仓体检 portfolio 纯函数单测：健康灯规则 / 预警汇总排序 / 总浮盈加权。

零依赖，可直接运行：python -m tests.test_portfolio
"""

from __future__ import annotations

import app.strategy.portfolio as P


def _row(**over) -> dict:
    base = {"price": 100, "stop_loss": None, "above_ma20": True, "main_flow_3d": 1.0,
            "is_holding": False, "pnl": None, "events": {}}
    base.update(over)
    return base


def test_health_green() -> None:
    lvl, flags = P._health(_row())
    assert lvl == "green" and not flags


def test_health_stop_break_red() -> None:
    lvl, flags = P._health(_row(price=95, stop_loss=100))
    assert lvl == "red" and any("止损" in f["text"] and f["level"] == "danger" for f in flags)


def test_health_break_ma_plus_outflow_red() -> None:
    lvl, _ = P._health(_row(above_ma20=False, main_flow_3d=-1.0))   # 破位+流出 → 红
    assert lvl == "red"


def test_health_break_only_yellow() -> None:
    lvl, flags = P._health(_row(above_ma20=False, main_flow_3d=2.0))  # 仅破位·资金还在 → 黄
    assert lvl == "yellow" and any("破位" in f["text"] for f in flags)


def test_health_events_yellow() -> None:
    _, f1 = P._health(_row(events={"float": {"next_days": 10, "next_ratio": 3.0}}))
    assert any("解禁" in f["text"] for f in f1)
    _, f2 = P._health(_row(events={"holder_trade": {"de_count": 2, "in_count": 0}}))
    assert any("减持" in f["text"] for f in f2)
    _, f3 = P._health(_row(events={"block": {"premium_avg": -4.0, "count": 1, "amount_yi": 0.5}}))
    assert any("大宗" in f["text"] for f in f3)
    # 远期/小比例解禁不触发
    _, f4 = P._health(_row(events={"float": {"next_days": 200, "next_ratio": 3.0}}))
    assert not any("解禁" in f["text"] for f in f4)


def test_health_holding_loss_yellow() -> None:
    lvl, flags = P._health(_row(is_holding=True, pnl=-12.0))
    assert lvl == "yellow" and any("浮亏" in f["text"] for f in flags)


def test_collect_alerts_sort() -> None:
    rows = [
        {"ts_code": "1", "name": "A", "is_holding": False, "flags": [{"level": "warn", "text": "x"}]},
        {"ts_code": "2", "name": "B", "is_holding": True, "flags": [{"level": "danger", "text": "跌破止损"}]},
    ]
    a = P._collect_alerts(rows)
    assert a[0]["level"] == "danger" and a[0]["name"] == "B"    # 危险在前


def test_total_pnl_weighted() -> None:
    rows = [
        {"is_holding": True, "pnl": 10, "cost": 100, "shares": 100},   # 市值1万·盈+1000
        {"is_holding": True, "pnl": -5, "cost": 200, "shares": 50},    # 市值1万·亏-500
        {"is_holding": False, "pnl": 99, "cost": 1, "shares": 1},      # 自选·不计
    ]
    assert P._total_pnl(rows) == 2.5    # (1000-500)/20000*100


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
