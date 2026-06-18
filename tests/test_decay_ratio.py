"""
全市场板块衰减占比 calc_decay_ratio 单元测试（对标吴川 decay_ratio）。

零依赖，可直接运行：python -m tests.test_decay_ratio
"""

from __future__ import annotations

from app.sector_analyzer import calc_decay_ratio, _DECAY_HIGH, _DECAY_MID
from app.state import SectorStat


def _stats(phases: list[str]) -> list[SectorStat]:
    return [SectorStat(industry=f"板块{i}", phase=p) for i, p in enumerate(phases)]


def test_empty_returns_unknown() -> None:
    r = calc_decay_ratio([])
    assert r["n_total"] == 0 and r["decay_ratio"] is None and r["level"] == "unknown"


def test_ratio_and_counts() -> None:
    # 4 退潮 / 10 总 = 0.40 → normal（< 0.45）
    r = calc_decay_ratio(_stats(["退潮"] * 4 + ["升温"] * 3 + ["趋势"] * 3))
    assert r["n_decay"] == 4 and r["n_total"] == 10
    assert r["decay_ratio"] == 0.4 and r["level"] == "normal"


def test_defensive_threshold() -> None:
    # 7 退潮 / 10 = 0.70 ≥ 0.60 → defensive（对标吴川 0.71）
    r = calc_decay_ratio(_stats(["退潮"] * 7 + ["中性"] * 3))
    assert r["decay_ratio"] == 0.7 and r["level"] == "defensive"


def test_cautious_band() -> None:
    # 5 退潮 / 10 = 0.50，落在 [0.45, 0.60) → cautious
    r = calc_decay_ratio(_stats(["退潮"] * 5 + ["趋势"] * 5))
    assert _DECAY_MID <= r["decay_ratio"] < _DECAY_HIGH and r["level"] == "cautious"


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
