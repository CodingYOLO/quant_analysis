"""
选股池重点分 + 星标单测：分数有区分度(非饱和)、星标恰好 Top10、强者居前。

零依赖，可直接运行：python -m tests.test_pool_focus
"""

from __future__ import annotations

import app.strategy.stock_pool as SP


def _rec(rps, flow, heat, n_strat=1, vr=1.5, ma20=1, ma60=1, slope=1):
    strategies = (["breakout"] * n_strat) + ["theme_pick"]
    return {"ts_code": f"{rps}.SZ", "rps50": rps, "main_flow_3d": flow, "theme_heat": heat,
            "strategies": strategies, "vol_ratio": vr, "above_ma20": ma20,
            "above_ma60": ma60, "slope_up": slope, "focus_score": 0.0, "star": 0}


def test_vol_health_and_ma_score() -> None:
    assert SP._vol_health(1.5) == 1.0 and SP._vol_health(3.0) == 0.5 and SP._vol_health(6.0) == 0.2
    assert SP._ma_score({"above_ma20": 1, "above_ma60": 1, "slope_up": 1}) == 1.0
    assert SP._ma_score({"above_ma20": 1, "above_ma60": 0, "slope_up": 0}) == 0.5
    assert SP._ma_score({"above_ma20": 0, "above_ma60": 0, "slope_up": 0}) == 0.0


def test_focus_score_discriminates_and_stars_top10() -> None:
    # 15 只递减强度 + 1 只极弱 → 共 16 只
    recs = [_rec(rps=95 - i * 3, flow=10 - i, heat=90 - i * 3, n_strat=2) for i in range(15)]
    recs.append(_rec(rps=20, flow=-5, heat=10, n_strat=1, vr=6.0, ma20=0, ma60=0, slope=0))
    SP._compute_focus_scores(recs)

    scores = [r["focus_score"] for r in recs]
    assert len(set(scores)) > 5                 # 有区分度（不像 0.98 那样饱和）
    assert all(0 <= s <= 100 for s in scores)
    assert sum(r["star"] for r in recs) == 5    # 恰好星标 Top5

    strong = max(recs, key=lambda r: r["focus_score"])
    weak = min(recs, key=lambda r: r["focus_score"])
    assert strong["star"] == 1 and weak["star"] == 0    # 最强标星、最弱不标
    assert strong["rps50"] >= weak["rps50"]


def test_focus_score_fewer_than_5() -> None:
    recs = [_rec(rps=80 - i * 5, flow=3 - i, heat=60) for i in range(3)]
    SP._compute_focus_scores(recs)
    assert sum(r["star"] for r in recs) == 3     # 不足5只 → 全标
    assert SP._compute_focus_scores([]) is None  # 空安全


def test_open_gate_board_aware() -> None:
    strong = lambda: {"theme_heat": 85.0, "main_flow_3d": 5.0, "above_ma20": 1, "risk_flags": []}
    weak = lambda: {"theme_heat": 50.0, "main_flow_3d": -1.0, "above_ma20": 0, "risk_flags": []}
    # 强势/震荡市：正常开仓
    assert SP._open_gate(strong(), "震荡") == (True, 0.03)
    assert SP._open_gate(strong(), "主升") == (True, 0.05)
    # 弱市 + 强板块龙头(热度≥70+资金流入+多头) → 可做·试仓3%
    ok, pos = SP._open_gate(strong(), "弱势")
    assert ok is True and pos == SP._WEAK_TRIAL_POS
    # 弱市 + 弱板块 → 不开(观察)
    assert SP._open_gate(weak(), "弱势")[0] is False
    # 弱市 + 热度够但资金流出 → 不开(必须真有资金)
    r = strong(); r["main_flow_3d"] = -0.5
    assert SP._open_gate(r, "弱势")[0] is False
    # 数据缺失 → 一律不开
    assert SP._open_gate(strong(), "数据缺失")[0] is False


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
