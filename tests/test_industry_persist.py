"""行业「资金持续流入榜」核心指标·纯函数测试。

重点：连续净流入天数(从末往回)、近5/近10累计、流入天数、暗流(资金进价没涨)、缺失日(None)处理。
运行：.venv/bin/python tests/test_industry_persist.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.industry_flow import _compound_pct, _series_metrics  # noqa: E402


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


# ── 连续净流入天数：从最新往回数连续 >0 的天数（一旦断=停）──────────────────────────
def test_consec_days_from_end() -> None:
    nets = [1.0, -2.0, 3.0, 4.0, 5.0]                 # 末3天连续为正
    m = _series_metrics(nets, [0.0] * 5)
    _assert(m["consec_days"] == 3, f"连续应=3(末3天>0)，实得 {m['consec_days']}")
    # 最新一天转负 → 连续=0
    _assert(_series_metrics([1, 2, 3, -1], [0] * 4)["consec_days"] == 0, "最新为负→连续0")
    # 全为正 → 连续=全长
    _assert(_series_metrics([1, 2, 3], [0] * 3)["consec_days"] == 3, "全正→连续=长度")
    print("  ✓ 连续净流入天数：从末往回·遇断即停")


# ── 累计：近5取末5、近10取全部；缺失日(None)跳过不当0 ──────────────────────────────
def test_cumulative_windows() -> None:
    nets = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]  # 10天
    m = _series_metrics(nets, [0] * 10)
    _assert(m["cum5"] == 400, f"近5=末5(60+70+80+90+100=400)，实得 {m['cum5']}")
    _assert(m["cum10"] == 550, f"近10=全部(550)，实得 {m['cum10']}")
    _assert(m["days_in"] == 10 and m["n_days"] == 10, "流入天数/窗口应=10")
    # 含缺失日：None 不计入累计、不计入流入天数
    m2 = _series_metrics([None, 5.0, None, 5.0, 5.0], [0] * 5)
    _assert(m2["cum5"] == 15.0, f"缺失日跳过·累计应=15，实得 {m2['cum5']}")
    _assert(m2["days_in"] == 3, f"流入天数应=3(仅3个>0)，实得 {m2['days_in']}")
    _assert(m2["consec_days"] == 2, f"末2天>0(中间None不影响末段)，实得 {m2['consec_days']}")
    print("  ✓ 累计窗口 + 缺失日(None)跳过")


# ── 暗流：连续进≥2天 且 近5累计>0 且 板块近5日涨幅<3%（资金进但价没涨=埋伏）────────────
def test_ambush_flag() -> None:
    # 连续5天净流入、但每天仅涨 0.1% → 近5涨≈0.5% <3% → 暗流
    quiet = _series_metrics([2, 2, 2, 2, 2], [0.1, 0.1, 0.1, 0.1, 0.1])
    _assert(quiet["ambush"] is True, f"资金持续进+价没涨应=暗流，实得 {quiet}")
    # 连续净流入但价大涨(每天+3%→近5≈15%) → 追高·非暗流
    chase = _series_metrics([2, 2, 2, 2, 2], [3, 3, 3, 3, 3])
    _assert(chase["ambush"] is False, "价大涨=追高·非暗流")
    # 只进1天(不连续) → 非暗流
    _assert(_series_metrics([-1, -1, -1, -1, 2], [0.1] * 5)["ambush"] is False, "仅1天进·非暗流")
    print("  ✓ 暗流：连续进+近5累计>0+价没涨(<3%)")


# ── 复利涨幅 + 边界 ────────────────────────────────────────────────────────────
def test_compound_and_boundary() -> None:
    _assert(_compound_pct([]) is None, "空→None")
    _assert(abs(_compound_pct([10, 10]) - 21.0) < 1e-6, f"1.1*1.1-1=21%，实得 {_compound_pct([10, 10])}")
    empty = _series_metrics([None, None], [None, None])
    _assert(empty["cum5"] == 0 and empty["consec_days"] == 0 and empty["ret5"] is None, "全缺→0/0/None")
    print("  ✓ 复利涨幅 + 全缺失边界")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n行业持续流入榜·指标测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
