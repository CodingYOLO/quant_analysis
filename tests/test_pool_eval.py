"""
选股池评分回测 pool_eval 单测：合成面板验证"强档跑赢弱档" + 前向聚合 + 总览。

零依赖，可直接运行：python -m tests.test_pool_eval
"""

from __future__ import annotations

import pandas as pd

import app.backtest.pool_eval as E


def test_stats_and_ramp() -> None:
    s = E._stats(pd.Series([5.0, -3.0, 8.0, -1.0]))
    assert s["n"] == 4 and s["win_rate"] == 50.0 and s["avg_return"] == 2.25
    assert E._stats(pd.Series([], dtype=float))["n"] == 0
    rv = E._ramp_vec(pd.Series([5, 8, 28, 40]), 8, 28)
    assert list(rv) == [0.0, 0.0, 1.0, 1.0]


def _trend_panel(n=60, m=80) -> pd.DataFrame:
    """n 只股、m 日：斜率从 -0.5→+0.5 线性递增。斜率越大 RPS 越高、未来越涨。"""
    dates = [f"2026{(t // 28) + 1:02d}{(t % 28) + 1:02d}" for t in range(m)]
    slopes = [-0.5 + i / (n - 1) for i in range(n)]
    data = {f"{600000 + i}.SH": [100 + slopes[i] * t for t in range(m)] for i in range(n)}
    return pd.DataFrame(data, index=dates).T   # index=ts_code, columns=日期


def test_eval_one_date_strong_beats_weak() -> None:
    panel = _trend_panel()
    cols = list(panel.columns)
    r = E._eval_one_date(panel, cols, 70)
    assert r and set(r["tiers"]) == {"强", "中", "弱"}
    # 上升股(高斜率)评分高且未来涨 → 强档胜率 > 弱档胜率
    assert r["tiers"]["强"]["win_rate"] > r["tiers"]["弱"]["win_rate"]
    assert r["spread"] is not None and r["spread"] > 0


def test_run_historical_smoke() -> None:
    class _FakeProv:  # build_qfq_panel 需要 provider，但我们直接 monkeypatch 面板
        pass
    panel = _trend_panel(m=90)
    E_build = __import__("app.factors.breadth_qfq", fromlist=["build_qfq_panel"])
    orig = E_build.build_qfq_panel
    E_build.build_qfq_panel = lambda end, prov, lookback=145: panel
    try:
        evals = E.run_historical(end_date="20260301", step=5, provider=_FakeProv())
        assert evals and all(e["source"] == "backtest" for e in evals)
        agg = E.aggregate(evals, "强", "弱")
        assert agg["n_days"] > 0 and agg["strong_win"] >= agg["weak_win"]   # 强档不输弱档
    finally:
        E_build.build_qfq_panel = orig


def test_eval_pool_date_forward() -> None:
    rows = [{"focus_score": 85, "star": 1, "t5_return": 6.0, "t3_return": 3.0},
            {"focus_score": 80, "star": 1, "t5_return": 4.0, "t3_return": 2.0},
            {"focus_score": 62, "star": 0, "t5_return": -2.0, "t3_return": -1.0},
            {"focus_score": 61, "star": 0, "t5_return": -3.0, "t3_return": 0.0},
            {"focus_score": 70, "star": 0, "t5_return": None}]   # 未到期 → 剔除
    r = E.eval_pool_date(rows)
    assert r and r["tiers"]["⭐重点"]["n"] == 2 and r["tiers"]["⭐重点"]["win_rate"] == 100.0
    assert r["tiers"]["高分(≥75)"]["win_rate"] > r["tiers"]["其余(<75)"]["win_rate"]
    assert r["spread"] > 0
    assert E.eval_pool_date([{"focus_score": 80, "star": 1, "t5_return": None}]) is None


def test_aggregate() -> None:
    evals = [{"tiers": {"强": {"win_rate": 70}, "弱": {"win_rate": 40}}},
             {"tiers": {"强": {"win_rate": 55}, "弱": {"win_rate": 60}}}]
    a = E.aggregate(evals, "强", "弱")
    assert a["n_days"] == 2 and a["strong_win"] == 62.5 and a["beat_ratio"] == 50.0


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
