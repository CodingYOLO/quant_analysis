"""板块诊断·状态机 + 指标·纯函数测试（状态判定 / F 标准化 / 滚动和 / 近5日涨幅 / 边界）。

运行：.venv/bin/python tests/test_sector_diagnosis.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.sector_diagnosis import (  # noqa: E402
    _rollsum, _ret5, _zscore_cross, classify_state)


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


# ── 顶背离：价涨 + 宽度高位 + 资金减速（F1d 转弱·F3d 走低）──────────────────────────
def test_state_top_divergence() -> None:
    m = {"ma5": 90, "ma20": 66, "ma60": 60, "ma5_prev": 92,
         "f1d": 1, "f1d_prev": 4, "f3d": 5, "f3d_prev": 12, "ret5": 6.0}
    _assert(classify_state(m) == "顶背离", f"价涨+宽度高+资金减速应=顶背离，实得 {classify_state(m)}")
    print("  ✓ 顶背离：价涨+宽度高位+资金加速度转负")


# ── 暗流：价平/跌 + F3d 持续为正且不减（资金逆价流入）──────────────────────────────
def test_state_ambush() -> None:
    m = {"ma5": 46, "ma20": 34, "ma60": 40, "ma5_prev": 40,
         "f1d": 5, "f1d_prev": 3, "f3d": 15, "f3d_prev": 11, "ret5": 0.5}
    _assert(classify_state(m) == "暗流", f"价没涨+资金持续进应=暗流，实得 {classify_state(m)}")
    # 价大涨 → 不是暗流（走顶背离或健康）
    m2 = {**m, "ret5": 8.0, "ma5": 90, "ma5_prev": 92, "f1d": 5, "f1d_prev": 3}
    _assert(classify_state(m2) != "暗流", "价大涨不应判暗流")
    print("  ✓ 暗流：价没涨(<阈值)+F3d持续为正")


# ── 高位回调：MA5 从高位破位、MA60 仍撑 ───────────────────────────────────────────
def test_state_pullback() -> None:
    m = {"ma5": 37, "ma20": 77, "ma60": 78, "ma5_prev": 75,
         "f1d": -2, "f1d_prev": 3, "f3d": 8, "f3d_prev": 15, "ret5": -3.0}
    _assert(classify_state(m) == "高位回调", f"MA5破位+MA60撑应=高位回调，实得 {classify_state(m)}")
    print("  ✓ 高位回调：短周期宽度破位·长周期仍撑")


# ── 洗盘谷底：MA5 极低 + 资金流出减速（F1d 由深负收窄）────────────────────────────
def test_state_washout() -> None:
    m = {"ma5": 14, "ma20": 25, "ma60": 30, "ma5_prev": 30,
         "f1d": -1, "f1d_prev": -6, "f3d": -3, "f3d_prev": -10, "ret5": -4.0}
    _assert(classify_state(m) == "洗盘谷底", f"宽度极低+流出减速应=洗盘谷底，实得 {classify_state(m)}")
    print("  ✓ 洗盘谷底：MA5极低+F1d由深负收窄")


# ── 缺失/中性：关键指标缺 → 中性（不瞎判）───────────────────────────────────────
def test_state_neutral_and_missing() -> None:
    _assert(classify_state({"ma5": None, "f1d": 1, "f3d": 1, "ret5": 1}) == "中性", "缺MA5→中性")
    _assert(classify_state({"ma5": 50, "ma20": 50, "ma60": 50, "ma5_prev": 50,
                            "f1d": 0, "f1d_prev": 0, "f3d": 0, "f3d_prev": 0, "ret5": 0.5}) == "中性",
            "平淡→中性")
    print("  ✓ 中性/缺失：关键指标缺→保守中性")


# ── F1d 横截面标准化：强流入板块 z 应显著为正、流出为负 ─────────────────────────────
def test_zscore_cross() -> None:
    z = _zscore_cross({"A": 100.0, "B": 0.0, "C": -100.0, "D": 5.0, "E": -5.0})
    _assert(z["A"] > 0 and z["C"] < 0, f"A应>0 C应<0，实得 {z}")
    _assert(_zscore_cross({}) == {}, "空→{}")
    print(f"  ✓ F1d横截面标准化：强流入>0·流出<0 ({z['A']}/{z['C']})")


# ── 滚动和 + 近5日涨幅 + 边界 ─────────────────────────────────────────────────
def test_rollsum_and_ret5() -> None:
    seq = [1, 2, 3, 4, 5]
    _assert(_rollsum(seq, 4, 3) == 12, f"末3和=12，实得 {_rollsum(seq, 4, 3)}")
    _assert(_rollsum([None, None, 3], 2, 3) == 3, "含None只求非空")
    _assert(_rollsum([None], 0, 3) is None, "全空→None")
    _assert(abs(_ret5([10, 10, 10, 10, 10], 4) - 61.05) < 0.1, f"5个+10%复利≈61%，实得 {_ret5([10]*5,4)}")
    _assert(_ret5([], 0) is None, "空→None")
    print("  ✓ 滚动和 + 近5日复利涨幅 + 边界")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n板块诊断·状态机测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
