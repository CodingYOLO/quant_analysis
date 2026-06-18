"""
人气排名改善 rank_delta 单元测试（对标吴川「人气排名改善_3日」）。

零依赖，可直接运行：python -m tests.test_pop_rank_improvement
"""

from __future__ import annotations

from app.factors.popularity import rank_delta


def test_improvement_positive_when_rank_moves_up() -> None:
    # 排名从 100 → 84（名次前移）→ 改善 = 100-84 = +16
    d = rank_delta(today_ranks={"A": 84}, prior_ranks={"A": 100})
    assert d["A"] == 16


def test_decline_negative() -> None:
    # 排名从 50 → 90（名次后退）→ -40
    d = rank_delta({"B": 90}, {"B": 50})
    assert d["B"] == -40


def test_missing_one_side_skipped() -> None:
    # 仅一期有排名 → 不计（避免伪造）
    d = rank_delta({"A": 10, "B": 20}, {"A": 30})
    assert d == {"A": 20} and "B" not in d


def test_empty() -> None:
    assert rank_delta({}, {}) == {}


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
