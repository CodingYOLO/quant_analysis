"""人气榜反转选股·纯函数单测（每条筛选分支 + 分档 + 双确认 + 排序 + 边界）。

运行：.venv/bin/python tests/test_hot_reversal.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.hot_reversal import DEFAULTS, screen_hot_reversal  # noqa: E402


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


def _traj(code, peak, trough, cur, fmv=200, name=None):
    return {"code": code, "name": name or code, "peak_rank": peak,
            "trough_rank": trough, "cur_rank": cur, "float_mv_yi": fmv}


def _lv(state):
    return {"position": {"state": state, "label": f"位置={state}"},
            "entry_zone": {"low": 9.0, "high": 10.0, "basis": "MA20"}}


# 一只标准通过票：峰值#30·谷值#500·当前#380 → 回升120·best·流通200亿·tech in
_PASS = _traj("000001", 30, 500, 380, fmv=200)
_LV_IN = {"000001": _lv("in")}


def test_standard_pass_best() -> None:
    r = screen_hot_reversal([_PASS], _LV_IN)
    _assert(len(r) == 1, "标准票应通过")
    c = r[0]
    _assert(c["recover"] == 120 and c["tier"] == "best", f"回升/分档错 {c}")
    _assert(c["tech_state"] == "in" and c["reasons"], "应带技术态+依据")
    print("  ✓ 标准通过·回升120·best·双确认in·带可溯源依据")


def test_tier_steady() -> None:
    # 回升30(<50)→steady·刚拐头
    r = screen_hot_reversal([_traj("t", 40, 420, 390)], {"t": _lv("watch")})
    _assert(r and r[0]["tier"] == "steady", "回升<50 应为 steady")
    print("  ✓ 回升<50 → steady(刚拐头)")


def test_reject_float_small() -> None:
    r = screen_hot_reversal([_traj("s", 30, 500, 380, fmv=50)], {"s": _lv("in")})
    _assert(not r, "流通<80亿应剔除")
    print("  ✓ 流通<80亿 → 剔除")


def test_reject_never_hot() -> None:
    r = screen_hot_reversal([_traj("n", 150, 500, 380)], {"n": _lv("in")})
    _assert(not r, "峰值>100(没真火过)应剔除")
    print("  ✓ 峰值>100(没进过Top100) → 剔除")


def test_reject_trough_window() -> None:
    cold = screen_hot_reversal([_traj("c", 30, 1200, 900)], {"c": _lv("in")})
    hot = screen_hot_reversal([_traj("h", 30, 200, 120)], {"h": _lv("in")})
    _assert(not cold, "谷值>800(太冷)应剔除")
    _assert(not hot, "谷值<300(还热)应剔除")
    print("  ✓ 谷值不在[300,800]洗盘窗口 → 剔除")


def test_reject_recover_edges() -> None:
    still = screen_hot_reversal([_traj("a", 30, 500, 495)], {"a": _lv("in")})   # 回升5<min
    fast = screen_hot_reversal([_traj("b", 30, 800, 300)], {"b": _lv("in")})    # 回升500>fast
    _assert(not still, "回升<下限(没拐头)应剔除")
    _assert(not fast, "回升>上限(太快·追高)应剔除")
    print("  ✓ 回升太小(没拐头)/太大(追高) → 剔除")


def test_double_confirm() -> None:
    far = screen_hot_reversal([_PASS], {"000001": _lv("far")})       # 技术远离
    na = screen_hot_reversal([_PASS], None)                          # 无关键位
    _assert(not far, "require_tech 下·技术far应剔除")
    _assert(not na, "require_tech 下·无关键位应剔除")
    loose = screen_hot_reversal([_PASS], None, {"require_tech": False})
    _assert(loose and loose[0]["tech_state"] == "na", "关闭双确认应放行")
    print("  ✓ 双确认：技术far/无数据剔除·关闭开关放行")


def test_sort_order() -> None:
    a = _traj("a", 30, 500, 400)   # 回升100 best
    b = _traj("b", 30, 500, 380)   # 回升120 best
    c = _traj("c", 30, 420, 390)   # 回升30 steady
    lm = {"a": _lv("in"), "b": _lv("in"), "c": _lv("in")}
    r = screen_hot_reversal([a, c, b], lm)
    _assert([x["code"] for x in r] == ["b", "a", "c"], f"排序错 {[x['code'] for x in r]}")
    print("  ✓ 排序：best优先·回升多者靠前(b>a>c)")


def test_boundary_missing() -> None:
    r = screen_hot_reversal([{"code": "x", "cur_rank": 300}], None, {"require_tech": False})
    _assert(not r, "缺峰值/谷值应跳过")
    _assert(screen_hot_reversal([], None) == [], "空输入→空")
    print("  ✓ 缺字段/空输入 → 安全跳过(边界)")


def test_defaults_sane() -> None:
    _assert(DEFAULTS["trough_lo"] < DEFAULTS["trough_hi"], "谷值窗口应有序")
    _assert(DEFAULTS["recover_min"] < DEFAULTS["recover_fast"], "回升窗口应有序")
    print("  ✓ 默认阈值自洽(待回测校准)")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n人气榜反转选股测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
