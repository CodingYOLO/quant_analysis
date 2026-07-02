"""板块诊断特征引擎·纯函数测试（分母异常 / 复权口径一致性 / MA暖机 / 横截面标准化 / 滚动·差分）。

对应用户"别等重跑完才发现"的工程自测要求。运行：.venv/bin/python tests/test_sector_metrics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.sector_metrics import (_breadth_panels, _cross_z,  # noqa: E402
                                         _diff, _rollsum, _safe_div)


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


# ── 分母异常：成交额/流通市值=0 或缺失 → None（不除零·不污染 F 值）─────────────────
def test_denominator_edge() -> None:
    _assert(_safe_div(5.0, 0) is None, "分母0→None(不除零)")
    _assert(_safe_div(5.0, None) is None, "分母缺失→None")
    _assert(_safe_div(None, 100.0) is None, "分子缺失→None")
    _assert(_safe_div(0.0, 100.0) == 0.0, "净流入0→0(正常)")
    v = _safe_div(3.0, 100.0)
    _assert(v is not None and abs(v - 3.0) < 1e-6, f"3/100*100=3%，实得 {v}")
    print("  ✓ 分母异常：0/缺失→None(不除零)·正常值正确")


# ── 复权口径一致性：MA 与 close 同口径 → 宽度对"整段价格缩放"不变（后复权=前复权等价）───
def test_breadth_scale_invariance() -> None:
    # 3 只股 × 30 日随机价，构造两档均线宽度
    rng = np.random.default_rng(1)
    dates = [f"2024{m:02d}{d:02d}" for m in (1, 2) for d in range(1, 16)][:30]
    base = pd.DataFrame(rng.uniform(10, 20, size=(3, 30)),
                        index=["s1", "s2", "s3"], columns=dates)
    above1, valid1 = _breadth_panels(base, (5, 20))
    # 每只股整体乘不同正因子（模拟不同复权基准）→ close≥MA 关系应完全不变
    scaled = base.mul(pd.Series({"s1": 2.0, "s2": 0.3, "s3": 100.0}), axis=0)
    above2, _ = _breadth_panels(scaled, (5, 20))
    for w in (5, 20):
        _assert(above1[w].equals(above2[w]), f"MA{w}宽度应对整段缩放不变(复权口径一致)")
    print("  ✓ 复权一致性：close≥MA 对每股整段缩放不变(后复权/前复权等价)")


# ── MA 暖机：不足 w 日的头部 → NaN·valid=False（不当作跌破稀释宽度）──────────────────
def test_ma_warmup() -> None:
    dates = [f"202401{d:02d}" for d in range(1, 26)]           # 25 日
    panel = pd.DataFrame(np.arange(25.0)[None, :].repeat(2, 0),
                         index=["a", "b"], columns=dates)       # 单调上行
    above, valid = _breadth_panels(panel, (5, 20))
    # MA20：前 19 日不足 → valid=False；第 20 日起有效
    _assert(not valid[20].iloc[0, :19].any().any() if hasattr(valid[20].iloc[0, :19], "any")
            else not valid[20].iloc[:, :19].values.any(), "MA20 前19日应暖机不足(valid=False)")
    _assert(valid[20].iloc[:, 19:].values.all(), "MA20 第20日起应有效")
    print("  ✓ MA暖机：不足w日→valid=False(不算残缺·不稀释宽度)")


# ── 横截面稳健标准化（中位/MAD）：强流入>0·流出<0·样本<5→None ─────────────────────
def test_cross_z() -> None:
    z = _cross_z({"A": [10.0], "B": [0.0], "C": [-10.0], "D": [1.0], "E": [-1.0]}, list("ABCDE"), 1)
    _assert(z["A"][0] > 0 and z["C"][0] < 0, f"A>0 C<0，实得 {z['A'][0]}/{z['C'][0]}")
    z2 = _cross_z({"A": [1.0], "B": [2.0]}, ["A", "B"], 1)      # <5 板块
    _assert(z2["A"][0] is None, "样本<5→None(不瞎标准化)")
    print("  ✓ 横截面稳健标准化：强流入>0/流出<0·小样本→None")


# ── 滚动和 / 差分（加速度）边界 ───────────────────────────────────────────────────
def test_roll_diff() -> None:
    _assert(_rollsum([1, 2, 3, 4], 3, 3) == 9, "末3和=9")
    _assert(_rollsum([None, 2, 3], 2, 3) == 5, "含None只求非空")
    _assert(_diff([1, 3], 1) == 2, "差分=2")
    _assert(_diff([None, 3], 1) is None, "前值缺→None")
    _assert(_diff([1, 3], 0) is None, "首位无前值→None")
    print("  ✓ 滚动和/加速度差分边界")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n特征引擎·工程自测（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
