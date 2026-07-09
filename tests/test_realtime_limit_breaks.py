"""开板/炸板预警·时段门禁纯函数测试。

回归 2026-07-09 踩坑：收盘后点『测试推送』(force=true) → detect_limit_breaks 在休市快照
(封单读数=0) 上误报一堆"封单萎缩至0%·随时炸板"。修法：非连续竞价时段直接跳过。

本测试双向证明：
  1) 连续竞价 + 封单萎缩 → 照常报开板预警（不误杀真信号）；
  2) 收盘后(session≠continuous) → 同样的萎缩数据一律不报，且封板集合原样保留（不被0污染）。

运行：.venv/bin/python tests/test_realtime_limit_breaks.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.realtime_fund import detect_limit_breaks  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _sealed_row_with_zero_seal() -> dict:
    """封在涨停但买一封单读数=0（休市/撤光的形态），成交额够大不被噪音过滤。"""
    return {"ts_code": "600000.SH", "name": "测试封板", "limit_up": 11.00,
            "price": 11.00, "bid_vol": [0], "amount": 2e8, "pct_chg": 10.0}


def _prev_peak(peak: float = 1_000_000.0) -> dict:
    return {"600000.SH": {"peak": peak, "name": "测试封板"}}


def test_continuous_still_warns() -> None:
    """连续竞价：封单从峰值萎缩到0 → 必须报开板预警（真信号不能被误杀）。"""
    events, new_sealed = detect_limit_breaks(
        [_sealed_row_with_zero_seal()], _prev_peak(), session="continuous")
    _assert(len(events) == 1, f"连续竞价应产 1 条开板预警，实得 {len(events)}")
    key, title, body, code = events[0]
    _assert("开板预警" in title, f"标题应含『开板预警』：{title}")
    _assert("封单萎缩" in body, f"正文应含『封单萎缩』：{body}")
    _assert(code == "600000.SH", "事件应带股票代码")


def test_closed_suppresses() -> None:
    """收盘后：同样的萎缩数据一律不报，且封板集合原样返回（不被0污染）。"""
    prev = _prev_peak()
    for sess in ("closed", "auction", "auction_lock", "pre_open"):
        events, new_sealed = detect_limit_breaks(
            [_sealed_row_with_zero_seal()], prev, session=sess)
        _assert(events == [], f"时段 {sess} 不应产任何开板/炸板事件，实得 {events}")
        _assert(new_sealed == prev, f"时段 {sess} 应原样保留封板集合，实得 {new_sealed}")


def test_closed_returns_copy_not_alias() -> None:
    """休市分支返回的应是副本：调用方 clear()+update() 不能把原集合清空（别名陷阱回归）。"""
    prev = _prev_peak()
    _, new_sealed = detect_limit_breaks([_sealed_row_with_zero_seal()], prev, session="closed")
    _assert(new_sealed is not prev, "休市分支应返回副本而非同一对象（否则 clear() 会连原集合一起清空）")
    # 模拟调用方回写逻辑：_sealed.clear(); _sealed.update(new_sealed)
    prev.clear()
    prev.update(new_sealed)
    _assert(prev.get("600000.SH", {}).get("peak") == 1_000_000.0, "回写后峰值应完好保留")


def test_no_prev_no_break() -> None:
    """无历史封板集合时，收盘后调用不崩、返回空。"""
    events, new_sealed = detect_limit_breaks([_sealed_row_with_zero_seal()], {}, session="closed")
    _assert(events == [] and new_sealed == {}, "空历史+休市应安全返回空")


def _run() -> None:
    tests = [test_continuous_still_warns, test_closed_suppresses,
             test_closed_returns_copy_not_alias, test_no_prev_no_break]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n✅ 全部 {len(tests)} 项通过")


if __name__ == "__main__":
    _run()
