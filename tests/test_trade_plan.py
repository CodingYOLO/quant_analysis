"""交易计划 trade_plan 单测：导出格式转换 + QMT 脚本完整性。

零依赖。直接运行：python -m tests.test_trade_plan
"""

from __future__ import annotations

import app.strategy.trade_plan as T


def test_to_qmt_plan_maps_and_filters() -> None:
    rows = [
        {"ts_code": "300308.SZ", "name": "中际旭创", "side": "buy", "buy_price": 265.0,
         "stop_loss": 250.0, "take_profit": 285.0, "position_pct": 0.1, "status": "pending"},
        {"ts_code": "600519.SH", "name": "贵州茅台", "side": "buy", "buy_price": 1600,
         "stop_loss": 1500, "position_pct": 0.2, "status": "done"},        # 非pending→剔除
        {"ts_code": "", "name": "空码", "status": "pending"},                # 无码→剔除
    ]
    out = T.to_qmt_plan(rows)
    assert len(out) == 1
    o = out[0]
    assert o["code"] == "300308.SZ" and o["action"] == "buy"
    assert o["buy_high"] == 265.0 and o["stop_loss"] == 250.0 and o["position_pct"] == 0.1


def test_to_qmt_plan_empty() -> None:
    assert T.to_qmt_plan([]) == []


def test_qmt_script_present() -> None:
    s = T.QMT_SCRIPT
    assert "xtquant" in s and "order_stock" in s and "plan.json" in s
    assert "模拟盘" in s            # 必含安全提示


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_trade_plan 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
