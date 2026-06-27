"""全市场盘中提醒：事件检测纯函数单测（零网络）。"""

from __future__ import annotations

from app.strategy.market_alert import detect_market_events


def _radar(hot, limit_up=10):
    return {"hot_sectors": hot, "weak_sectors": [],
            "breadth": {"total": 5000, "up": 2000, "down": 2800, "limit_up": limit_up, "limit_down": 5}}


def _sec(ind, avg, lead="龙头甲", lpct=5.0, lu=2):
    return {"industry": ind, "n": 20, "avg_pct": avg, "up": 10, "limit_up": lu,
            "leader": lead, "leader_code": "000001.SZ", "leader_pct": lpct}


def test_sector_weak_to_strong() -> None:
    """开盘弱(-1.5%)、现强(+1.0%) → 弱转强；开盘就强的不算。"""
    radar = _radar([_sec("光伏", 1.0), _sec("半导体", 3.0)])
    open_pct = {"光伏": -1.5, "半导体": 2.5}
    events, _, _ = detect_market_events(radar, open_pct, limit_max=20, first_scan=False, now_hm="1030")
    keys = {k for k, _, _ in events}
    assert "flip_光伏" in keys                 # 弱转强
    assert "flip_半导体" not in keys           # 开盘就强·不算转强


def test_hot_sector_and_limit_surge() -> None:
    radar = _radar([_sec("化学制药", 3.2)], limit_up=55)
    events, _, lu = detect_market_events(radar, {}, limit_max=40, first_scan=False, now_hm="1030")
    keys = {k for k, _, _ in events}
    assert "hot_化学制药" in keys              # 均涨≥2.5 强势热点
    assert "limit_50" in keys and "limit_30" not in keys   # 涨停过50档(40→55)·30档已过不重复
    assert lu == 55


def test_limit_surge_pushes_only_highest_crossed() -> None:
    """首扫 limit_max=0、涨停77 → 同时过30/50 档，只推最高的 limit_50（不刷屏）。"""
    radar = _radar([], limit_up=77)
    events, _, _ = detect_market_events(radar, {}, limit_max=0, first_scan=False, now_hm="1030")
    limit_keys = [k for k, _, _ in events if k.startswith("limit_")]
    assert limit_keys == ["limit_50"]


def test_auction_only_first_scan_near_open() -> None:
    radar = _radar([_sec("半导体", 1.2)])
    e1, _, _ = detect_market_events(radar, {}, 0, first_scan=True, now_hm="0926")
    assert any(k == "auction" for k, _, _ in e1)          # 首扫·开盘附近 → 集合竞价快照
    e2, _, _ = detect_market_events(radar, {}, 0, first_scan=True, now_hm="1030")
    assert not any(k == "auction" for k, _, _ in e2)       # 首扫但已过开盘 → 不算集合竞价


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_market_alert 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
