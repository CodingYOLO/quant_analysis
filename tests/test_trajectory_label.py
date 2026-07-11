"""板块资金"资金趋势"持续性标签·纯函数测试。

trajectory_label 把近程主动净买序列判成形态标签，供实时盯盘「资金趋势」列用。
关键：拐头流入(机会)/加速流入/冲高回落(避雷) 三档判准，数据不足安全返空。

运行：.venv/bin/python tests/test_trajectory_label.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.realtime_fund import trajectory_label  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _check(series, expect_key, name):
    r = trajectory_label(series)
    _assert(r["key"] == expect_key, f"{name}: 期望 key={expect_key}，实得 {r['key']}（label={r['label']}）")


def test_turn_is_opportunity():
    """由流出/零 转为流入 → 拐头流入（拐点·机会）。"""
    _check([-3, -2, -1, 0, 2, 4, 6], "turn", "负转正")


def test_accel():
    """净流入且近段明显加速 → 加速流入。"""
    _check([1, 2, 3, 5, 8, 12], "accel", "加速")


def test_fade_is_warning():
    """曾冲高、现明显低于峰值 → 冲高回落（避雷）；含从高位一路下滑。"""
    _check([12, 14, 15, 9, 5, 3], "fade", "冲高后回落")
    _check([10, 9, 7, 5, 4, 3], "fade", "高位一路下滑")


def test_steady():
    """净流入平稳、无明显加速/回落 → 平稳流入。"""
    _check([4, 3, 4, 3, 4, 4], "steady", "平稳")


def test_outflow():
    """始终净流出 → 净流出。"""
    _check([-1, -2, -3, -4], "out", "净流出")


def test_insufficient_safe():
    """样本不足 → 空标签、安全（前端显 — 退回5分钟Δ）。"""
    r = trajectory_label([1, 2])
    _assert(r["key"] == "na" and r["label"] == "", "不足样本应返 na/空")
    _assert(trajectory_label([])["key"] == "na", "空序列应安全")
    _assert(trajectory_label([None, None, 1, 2])["key"] == "na", "含None且有效不足应安全")


def _run():
    tests = [test_turn_is_opportunity, test_accel, test_fade_is_warning,
             test_steady, test_outflow, test_insufficient_safe]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n✅ 全部 {len(tests)} 项通过")


if __name__ == "__main__":
    _run()
