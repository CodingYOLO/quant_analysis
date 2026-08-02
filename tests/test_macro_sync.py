"""宏观面板 · 取数编排纯函数测试（交易日对齐 / lag 可见性 / 结转上限 / NULL 语义）。

核心守住：**回看模式的 point-in-time 正确性由 align_to_trading_days 决定**——
1 月的 CPI 绝不能出现在 1 月的面板上。

运行：.venv/bin/python tests/test_macro_sync.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.macro.adapters.base import Point               # noqa: E402
from app.macro.sync import align_to_trading_days        # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


DAYS = ["20260206", "20260209", "20260210", "20260211", "20260212", "20260213"]


def _pt(as_of: str, v: float) -> Point:
    return Point(code="x", as_of=as_of, value=v, source="test")


# ── lag 可见性：as_of + lag_days 之前绝不可见（月频 point-in-time 的根） ────────
def test_lag_visibility() -> None:
    pts = [_pt("20251231", 0.1), _pt("20260131", 0.2)]      # 12月值 / 1月值
    rows = align_to_trading_days(pts, DAYS, lag_days=10, max_carry_days=45)
    by = {r["trade_date"]: r for r in rows}
    _assert(by["20260209"]["as_of"] == "20251231",
            "⭐2/9 只能看到12月值(1月值2/10才可见)——否则回看模式有前视偏差")
    _assert(by["20260210"]["as_of"] == "20260131" and by["20260210"]["value"] == 0.2,
            "2/10(=1/31+10天) 起才能看到1月值")
    _assert(by["20260210"]["is_stale"] == 0, "发布日当天是新值·stale=0")
    _assert(by["20260211"]["is_stale"] == 1 and by["20260211"]["value"] == 0.2,
            "发布次日起为结转值·stale=1 但 value 保留")
    print("  ✓ lag 可见性：发布前不可见·发布日stale=0·此后结转stale=1")


# ── 结转上限：超过 max_carry_days 必须回落 NULL ────────────────────────────
def test_carry_limit() -> None:
    pts = [_pt("20260206", 1.0)]
    rows = align_to_trading_days(pts, DAYS, lag_days=0, max_carry_days=3)
    by = {r["trade_date"]: r for r in rows}
    _assert(by["20260209"]["value"] == 1.0 and by["20260209"]["is_stale"] == 1,
            "3天内允许结转(显式降级·stale=1)")
    _assert(by["20260212"]["value"] is None and by["20260212"]["is_stale"] == 1,
            "⭐超过 max_carry_days 必须写 NULL——不能无限期挂着陈旧值冒充当前值")
    _assert(by["20260212"]["as_of"] == "20260206", "NULL 行仍保留 as_of(供排查为什么断供)")
    strict = align_to_trading_days(pts, DAYS, lag_days=0, max_carry_days=0)
    sby = {r["trade_date"]: r for r in strict}
    _assert(sby["20260206"]["value"] == 1.0 and sby["20260209"]["value"] is None,
            "max_carry_days=0 严格模式：只有数据当日有值")
    print("  ✓ 结转上限：限内显式降级·超限回落 NULL·0=严格模式")


# ── 无观测：全 NULL，绝不猜 ────────────────────────────────────────────────
def test_no_observation() -> None:
    rows = align_to_trading_days([], DAYS, lag_days=0, max_carry_days=45)
    _assert(all(r["value"] is None for r in rows), "无观测 → 全 NULL")
    _assert(all(r["is_stale"] == 0 for r in rows), "从未有过值时不算 stale(是缺数)")
    print("  ✓ 无观测：全 NULL·不猜")


# ── 同日多观测：取最新一条（观测按 as_of 排序后推进） ────────────────────────
def test_multiple_observations_order() -> None:
    pts = [_pt("20260209", 2.0), _pt("20260206", 1.0)]     # 故意乱序传入
    rows = align_to_trading_days(pts, DAYS, lag_days=0, max_carry_days=45)
    by = {r["trade_date"]: r for r in rows}
    _assert(by["20260206"]["value"] == 1.0, "2/6 只能看到2/6的值")
    _assert(by["20260210"]["value"] == 2.0 and by["20260210"]["as_of"] == "20260209",
            "2/10 应取到最新可见观测(2/9)")
    print("  ✓ 多观测：乱序输入·按可见时点正确推进")


if __name__ == "__main__":
    print("宏观面板 · 取数编排测试")
    for fn in (test_lag_visibility, test_carry_limit, test_no_observation,
               test_multiple_observations_order):
        fn()
    print("✅ 全部通过")
