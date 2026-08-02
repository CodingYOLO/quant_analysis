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
    rows = align_to_trading_days(pts, DAYS, lag_days=10, max_carry_days=45, freq="monthly")
    by = {r["trade_date"]: r for r in rows}
    _assert(by["20260209"]["as_of"] == "20251231",
            "⭐2/9 只能看到12月值(1月值2/10才可见)——否则回看模式有前视偏差")
    _assert(by["20260210"]["as_of"] == "20260131" and by["20260210"]["value"] == 0.2,
            "2/10(=1/31+10天) 起才能看到1月值")
    _assert(by["20260210"]["is_stale"] == 0, "发布日当天是新值·stale=0")
    _assert(by["20260211"]["is_stale"] == 1 and by["20260211"]["value"] == 0.2,
            "发布次日起为结转·is_stale=已沿用会话数(1) 且 value 保留")
    _assert(by["20260209"]["is_stale"] >= 1, "12月值到2/9已沿用多个会话·is_stale≥1")
    print("  ✓ lag 可见性：发布前不可见·发布日stale=0·此后按会话数递增")


# ── 结转上限：超过 max_carry_days 必须回落 NULL ────────────────────────────
def test_carry_limit() -> None:
    """daily 结转按**交易日会话**计——2/6(五)→2/9(一)只隔1个会话，自然日计数会把
    每个周一都误判超限(周五值+3自然日)。"""
    pts = [_pt("20260206", 1.0)]
    rows = align_to_trading_days(pts, DAYS, lag_days=0, max_carry_days=2, freq="daily")
    by = {r["trade_date"]: r for r in rows}
    _assert(by["20260209"]["value"] == 1.0 and by["20260209"]["is_stale"] == 1,
            "⭐周五值沿用到周一=1个会话(自然日算法会误判为3天)")
    _assert(by["20260210"]["value"] == 1.0 and by["20260210"]["is_stale"] == 2,
            "第2个会话仍在限内(带病展示)")
    _assert(by["20260211"]["value"] is None and by["20260211"]["is_stale"] == 3,
            "⭐第3个会话超限→NULL——日频断2个会话不是延迟是源坏了·要早知道")
    _assert(by["20260211"]["as_of"] == "20260206", "NULL 行仍保留 as_of(供排查断供起点)")
    strict = align_to_trading_days(pts, DAYS, lag_days=0, max_carry_days=0, freq="daily")
    sby = {r["trade_date"]: r for r in strict}
    _assert(sby["20260206"]["value"] == 1.0 and sby["20260209"]["value"] is None,
            "max_carry_days=0 严格模式：只有数据当日有值")
    print("  ✓ 结转上限：daily按会话计·周一不误杀·超限NULL·0=严格模式")


# ── 无观测：全 NULL，绝不猜 ────────────────────────────────────────────────
def test_no_observation() -> None:
    rows = align_to_trading_days([], DAYS, lag_days=0, max_carry_days=45, freq="daily")
    _assert(all(r["value"] is None for r in rows), "无观测 → 全 NULL")
    _assert(all(r["is_stale"] == 0 for r in rows), "从未有过值时不算 stale(是缺数)")
    print("  ✓ 无观测：全 NULL·不猜")


# ── 同日多观测：取最新一条（观测按 as_of 排序后推进） ────────────────────────
def test_multiple_observations_order() -> None:
    pts = [_pt("20260209", 2.0), _pt("20260206", 1.0)]     # 故意乱序传入
    rows = align_to_trading_days(pts, DAYS, lag_days=0, max_carry_days=45, freq="daily")
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
