"""活跃度排名·混合排名纯函数测试（微盘降权 / 巨头不霸榜 / 边界）。

运行：.venv/bin/python tests/test_activity_rank.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.activity_rank import _blend_rank  # noqa: E402


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


def _df(rows):
    return pd.DataFrame(rows, columns=["ts_code", "turnover_rate", "circ_mv"])


def test_microcap_demoted() -> None:
    # 双高(换手+成交额均靠前)应居首；纯微盘(高换手/成交额垫底)、沉睡巨头(高成交额/换手垫底)都不该登顶
    df = _df([("BALANCED", 25.0, 1000.0), ("MICRO.BJ", 60.0, 10.0),
              ("BIGSLEEPY", 1.0, 100000.0), ("MID.SZ", 20.0, 500.0)])
    r = _blend_rank(df)
    order = r["ts_code"].tolist()
    _assert(order[0] == "BALANCED", f"双高者应居首 {order}")
    _assert(order[0] != "MICRO.BJ", "纯微盘高换手不应登顶")
    print(f"  ✓ 双高居首·微盘/沉睡巨头不霸榜（{order}）")


def test_blend_is_rank_sum() -> None:
    # A: 换手第1·成交额第2 → 和3 ; B: 换手第2·成交额第1 → 和3 ; C: 都第3 → 和6
    df = _df([("A", 30.0, 100.0), ("B", 20.0, 200.0), ("C", 5.0, 50.0)])
    r = _blend_rank(df)
    _assert(r["ts_code"].tolist()[-1] == "C", "综合最弱者垫底")
    print("  ✓ 混合=换手排名+成交额排名之和")


def test_filters_and_boundary() -> None:
    df = _df([("OK.SZ", 10.0, 500.0), ("NANTR.SZ", float("nan"), 500.0),
              ("ZERO.SZ", 0.0, 500.0), ("NOMV.SZ", 10.0, 0.0)])
    r = _blend_rank(df)
    codes = r["ts_code"].tolist()
    _assert(codes == ["OK.SZ"], f"NaN/零换手/零流通应剔除 {codes}")
    _assert(_blend_rank(None) is None and _blend_rank(_df([])) is None, "空→None")
    print("  ✓ 剔除 NaN/零换手/零流通 + 空输入→None(边界)")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n活跃度排名测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
