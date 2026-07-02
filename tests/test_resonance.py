"""共振确定性选股·纯打分单测（4维命中 / 分级A-D / 位置分级 / 排序 / 相对板块阈值 / 边界）。

运行：.venv/bin/python tests/test_resonance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.resonance import _grade, score_resonance  # noqa: E402


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


def _rec(code, heat=30.0, inst=None, days=0, name=None):
    return {"ts_code": code, "name": name or code, "industry": "半导体",
            "theme_heat": heat, "inst_net_yi": inst, "inst_buy_days": days, "close": 10.0}


def _lv(state):
    return {"position": {"state": state, "label": f"位置={state}"},
            "entry_zone": {"low": 9.0, "high": 10.0, "basis": "MA20"}}


def _fin(roe, yoy):
    return {"roe": roe, "yoy": yoy}


def test_all_four_grade_a() -> None:
    # 板块强(heat=30≥中位) + 真钱(inst>0) + 入局到位(in) + 基本面(ROE12/增长)
    recs = [_rec("A", heat=30, inst=1.5, days=2)]
    r = score_resonance(recs, {"A": _lv("in")}, {"A": _fin(12, 30)})[0]
    _assert(r["resonance"] == 4, f"应4维全中 {r['resonance']}")
    _assert(r["grade"] == "A", f"共振强+到位应A {r['grade']}")
    _assert(all(r["dims"][k]["hit"] for k in ("sector", "realmoney", "entry", "fundamental")), "四维都应hit")
    print("  ✓ 4维全中 + 入局到位 → 共振4·A级")


def test_three_dims_far_grade_b() -> None:
    # 3维中(板块/真钱/基本面)·但位置far → B(等回踩·别追)
    recs = [_rec("B", heat=30, inst=1.0, days=1)]
    r = score_resonance(recs, {"B": _lv("far")}, {"B": _fin(15, 20)})[0]
    _assert(r["resonance"] == 3 and r["grade"] == "B", f"共振3+far应B {r}")
    print("  ✓ 共振3 + 远离 → B级(等回踩·别追)")


def test_two_dims_grade_c() -> None:
    recs = [_rec("C", heat=30, inst=None)]                 # 板块中·无真钱
    r = score_resonance(recs, {"C": _lv("in")}, {"C": _fin(3, -10)})[0]  # 基本面差
    _assert(r["resonance"] == 2 and r["grade"] == "C", f"应2维C {r}")
    print("  ✓ 2维 → C级")


def test_weak_grade_d() -> None:
    recs = [_rec("D", heat=5, inst=None)]                  # 板块弱(<中位)
    r = score_resonance(recs, {"D": _lv("far")}, {"D": _fin(2, -5)})[0]
    _assert(r["resonance"] <= 1 and r["grade"] == "D", f"弱应D {r}")
    print("  ✓ ≤1维 → D级")


def test_sector_relative_median() -> None:
    # 中位=20：heat30命中·heat10不命中
    recs = [_rec("hi", heat=30), _rec("mid", heat=20), _rec("lo", heat=10)]
    r = {x["ts_code"]: x for x in score_resonance(recs, {}, {})}
    _assert(r["hi"]["dims"]["sector"]["hit"], "heat≥中位应命中")
    _assert(r["mid"]["dims"]["sector"]["hit"], "heat=中位应命中")
    _assert(not r["lo"]["dims"]["sector"]["hit"], "heat<中位不命中")
    print("  ✓ 板块强势=相对池内中位(抗量纲)")


def test_realmoney_only_positive() -> None:
    pos = score_resonance([_rec("p", inst=0.8, days=2)], {}, {})[0]
    neg = score_resonance([_rec("n", inst=-0.5, days=1)], {}, {})[0]
    _assert(pos["dims"]["realmoney"]["hit"], "机构净买>0命中")
    _assert(not neg["dims"]["realmoney"]["hit"], "机构净卖不命中")
    print("  ✓ 真钱维度：仅机构净买>0命中(净卖/无 不算)")


def test_fundamental_thresholds() -> None:
    good = score_resonance([_rec("g")], {}, {"g": _fin(10, 5)})[0]     # ROE10≥8且增长
    lowroe = score_resonance([_rec("l")], {}, {"l": _fin(5, 5)})[0]    # ROE5<8
    decline = score_resonance([_rec("d")], {}, {"d": _fin(15, -3)})[0]  # 增速<0
    _assert(good["dims"]["fundamental"]["hit"], "ROE达标+增长命中")
    _assert(not lowroe["dims"]["fundamental"]["hit"], "ROE不足不命中")
    _assert(not decline["dims"]["fundamental"]["hit"], "净利下滑不命中")
    print("  ✓ 基本面：ROE≥8 且 净利同比>0 才命中")


def test_sort_order() -> None:
    recs = [_rec("x", heat=30, inst=1.0, days=1), _rec("y", heat=30, inst=2.0, days=2),
            _rec("z", heat=5)]
    lm = {"x": _lv("far"), "y": _lv("in"), "z": _lv("na")}
    fm = {"x": _fin(12, 10), "y": _fin(12, 10), "z": _fin(1, -1)}
    order = [c["ts_code"] for c in score_resonance(recs, lm, fm)]
    _assert(order[0] == "y", "共振高+到位者最前")
    _assert(order[-1] == "z", "最弱者最后")
    print(f"  ✓ 排序：共振↓→位置↓→真钱↓（{order}）")


def test_grade_helper() -> None:
    p = {"grade_a_dims": 3}
    _assert(_grade(4, "in", p) == "A" and _grade(3, "far", p) == "B", "A/B 边界")
    _assert(_grade(2, "in", p) == "C" and _grade(1, "in", p) == "D", "C/D 边界")
    print("  ✓ 分级阈值边界")


def test_boundary() -> None:
    _assert(score_resonance([], None, None) == [], "空输入→空")
    _assert(score_resonance([{"name": "无code"}], None, None) == [], "缺 ts_code 跳过")
    print("  ✓ 空/缺字段 安全(边界)")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n共振确定性选股测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
