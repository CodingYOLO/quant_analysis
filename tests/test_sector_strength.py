"""板块强弱总览单测：形态判定 / 行业聚合 + 龙头（零网络·合成因子表）。"""

from __future__ import annotations

import pandas as pd

import app.strategy.sector_strength as SS


# ---------------------------------------------------------------------------
# 1. 板块形态判定 _sector_phase
# ---------------------------------------------------------------------------

def test_phase_strong_dip_lowbuy() -> None:
    assert SS._sector_phase(avg_rps=70, avg_ret5=-5, ma60_pct=80) == "💎强势回调·可低吸"


def test_phase_strong_rally() -> None:
    assert SS._sector_phase(avg_rps=65, avg_ret5=4, ma60_pct=70) == "🔥强势领涨"


def test_phase_weak_breakdown_is_knife() -> None:
    assert "破位" in SS._sector_phase(avg_rps=30, avg_ret5=-2, ma60_pct=30)


def test_phase_neutral() -> None:
    assert SS._sector_phase(avg_rps=50, avg_ret5=0, ma60_pct=50) == "⚪中性/震荡"


# ---------------------------------------------------------------------------
# 2. 行业聚合 + 龙头 _aggregate_sectors
# ---------------------------------------------------------------------------

def _row(ind, rps, ret5, ma60, mnet, lead, name, code):
    return {"industry": ind, "rps120": rps, "ret5": ret5, "ret20": ret5,
            "above_ma60": ma60, "main_net_amount": mnet, "leader_score": lead,
            "name": name, "ts_code": code}


def test_aggregate_sorts_and_picks_leaders() -> None:
    df = pd.DataFrame([
        _row("强行业", 80, -5, True, 1.0, 90, "龙一", "001.SZ"),
        _row("强行业", 70, -4, True, 0.5, 80, "龙二", "002.SZ"),
        _row("强行业", 60, -6, True, -0.2, 70, "小弟", "003.SZ"),
        _row("弱行业", 30, -2, False, -1.0, 50, "弱一", "004.SZ"),
        _row("弱行业", 25, -3, False, -0.5, 40, "弱二", "005.SZ"),
        _row("弱行业", 35, -1, True, 0.1, 45, "弱三", "006.SZ"),
    ])
    out = SS._aggregate_sectors(df, min_n=3, top_leaders=2)
    assert [s["industry"] for s in out] == ["强行业", "弱行业"]      # 按avg_rps降序
    strong = out[0]
    assert strong["avg_rps"] == 70.0 and strong["ma60_pct"] == 100.0
    assert strong["phase"] == "💎强势回调·可低吸"
    assert [l["name"] for l in strong["leaders"]] == ["龙一", "龙二"]   # 龙头分前2·剔小弟
    assert "破位" in out[1]["phase"]


def test_aggregate_skips_thin_sectors() -> None:
    """成分数<min_n 的行业不出现。"""
    df = pd.DataFrame([_row("迷你", 90, 1, True, 1, 90, "x", "x"),
                       _row("迷你", 80, 1, True, 1, 80, "y", "y")])
    assert SS._aggregate_sectors(df, min_n=3) == []


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_sector_strength 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
