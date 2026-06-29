"""盘中"变化"追踪：板块加速/轮动 + 大盘趋势 + 市场快照 + 重大变化（纯函数·不连网）。"""

from __future__ import annotations

from app.strategy.realtime_fund import (_accel_tag, breadth_trend, detect_market_shifts,
                                        detect_theme_fermentation, market_pulse_text,
                                        sector_flow_delta)


def test_accel_tag() -> None:
    assert _accel_tag(30, 5, th=2) == "加速流入"
    assert _accel_tag(30, -5, th=2) == "流入放缓"
    assert _accel_tag(-30, -5, th=2) == "加速流出"
    assert _accel_tag(-30, 5, th=2) == "流出放缓"
    assert _accel_tag(30, 0.5, th=2) == ""          # 变化不足阈值


def _secs():
    return [{"industry": "半导体", "net_yi": 50.0, "avg_pct": 2.2, "leader": "寒武纪",
             "leader_code": "688256.SH", "leader_pct": 6.0},
            {"industry": "银行", "net_yi": -20.0, "avg_pct": -0.5, "leader": "招商银行",
             "leader_code": "600036.SH", "leader_pct": -0.3}]


def test_sector_flow_delta() -> None:
    ago = {"半导体": 38.0, "银行": -10.0}            # 半导体5min前38亿→现50亿(+12加速)
    out = sector_flow_delta(_secs(), ago, th=2)
    semi = next(s for s in out if s["industry"] == "半导体")
    assert semi["net_delta"] == 12.0 and semi["accel"] == "加速流入"
    bank = next(s for s in out if s["industry"] == "银行")
    assert bank["net_delta"] == -10.0 and bank["accel"] == "加速流出"
    # 无历史 → delta None
    assert sector_flow_delta(_secs(), {})[0]["net_delta"] is None


def test_breadth_trend() -> None:
    up = breadth_trend({"up": 3000, "limit_up": 50}, {"up": 2700, "limit_up": 44}, up_th=200)
    assert up["dir"] == "up" and "走强" in up["text"]
    dn = breadth_trend({"up": 2400, "limit_up": 40}, {"up": 2800, "limit_up": 46}, up_th=200)
    assert dn["dir"] == "down" and "走弱" in dn["text"]
    flat = breadth_trend({"up": 2800, "limit_up": 45}, {"up": 2750, "limit_up": 44}, up_th=200)
    assert flat["dir"] == "flat" and flat["text"] == ""
    assert breadth_trend({}, {})["dir"] == "flat"


def test_market_pulse_text() -> None:
    sd = sector_flow_delta(_secs(), {"半导体": 38.0, "银行": -10.0}, th=2)
    bt = breadth_trend({"up": 3000, "limit_up": 50}, {"up": 2700, "limit_up": 44})
    txt = market_pulse_text({"up": 3000, "down": 2000, "limit_up": 50, "limit_down": 5}, bt, sd, "普涨")
    assert "大盘" in txt and "涨3000/跌2000" in txt
    assert "走强" in txt and "半导体" in txt and "加速流入" in txt
    assert "撤离" in txt and "银行" in txt
    assert " ▸ " in txt                              # 分块


def test_detect_market_shifts() -> None:
    sd = sector_flow_delta(_secs(), {"半导体": 38.0, "银行": -10.0}, th=2)   # 半导体+12亿
    ev = detect_market_shifts(sd, breadth_trend({}, {}), accel_th=8)
    keys = {k for k, *_ in ev}
    assert "shift_accel_半导体" in keys                # 12≥8 异常加速
    # 大盘转向
    up = detect_market_shifts([], {"dir": "up", "d_up": 400, "text": "走强(上涨+400·涨停+5)"}, mkt_th=300)
    assert any(k == "shift_mkt_up" for k, *_ in up)
    # 小变化不触发
    assert detect_market_shifts([], {"dir": "flat", "d_up": 50}) == []


def test_theme_dedup_overlap() -> None:
    """题材去重叠：同一拨医药票的子概念只留最强一个，腾位给不同方向。"""
    rows = [{"ts_code": f"P{i}.SH", "name": f"药{i}", "pct_chg": 9.0, "amount": 2e8} for i in range(5)]
    rows += [{"ts_code": f"S{i}.SH", "name": f"芯{i}", "pct_chg": 8.0, "amount": 2e8} for i in range(4)]
    cmap = {
        "创新药": [f"P{i}.SH" for i in range(5)],            # 5只药
        "仿制药": [f"P{i}.SH" for i in range(4)] + ["S0.SH"],  # 几乎同一拨药→应被去掉
        "半导体": [f"S{i}.SH" for i in range(4)],            # 不同方向→保留
    }
    out = detect_theme_fermentation(rows, cmap, min_hot=3, min_pct=5, min_amount=1e8, overlap_th=0.6)
    themes = [t["theme"] for t in out]
    assert "创新药" in themes and "半导体" in themes           # 两个不同方向都在
    assert "仿制药" not in themes                              # 与创新药高度重叠→去掉


def test_volume_surge() -> None:
    import pandas as pd

    from app.strategy.realtime_fund import volume_surge
    df = pd.DataFrame([
        {"ts_code": "A.SH", "name": "放量", "price": 10, "pct_chg": 5, "inner": 100, "outer": 300,
         "vol_ratio": 6.0, "amount": 3e8},
        {"ts_code": "B.SH", "name": "缩量", "price": 10, "pct_chg": 1, "inner": 100, "outer": 100,
         "vol_ratio": 0.8, "amount": 3e8},                                   # 量比不足→剔除
        {"ts_code": "C.SH", "name": "无量", "price": 10, "pct_chg": 5, "inner": 100, "outer": 100,
         "vol_ratio": 9.0, "amount": 1e6},                                   # 额不足→剔除
    ])
    out = volume_surge(df, min_vr=3.0, min_amount=1e8)
    assert len(out) == 1 and out[0]["name"] == "放量" and out[0]["vol_ratio"] == 6.0


def test_watch_dip_signal() -> None:
    from app.strategy.realtime_fund import watch_dip_signal
    tech = {"ma20": 100.0, "low20": 96.0}
    q = {"name": "雅克科技", "price": 101.0, "pct_chg": -3.0, "inner": 80, "outer": 120}   # 贴MA20+1%·当日跌3%·外盘60%
    sig = watch_dip_signal(q, tech, prev_price_5m=100.5)                # 近5min +0.5%(企稳)
    assert sig and sig["pos"] == "贴MA20" and sig["recent"] == 0.5 and sig["outer"] == 60
    # 还在快速下跌 → 不算企稳
    assert watch_dip_signal(q, tech, prev_price_5m=103.0) is None       # 5min内 -1.9%
    # 已大涨 → 不是低吸
    assert watch_dip_signal({**q, "pct_chg": 6.0}, tech, 100.5) is None
    # 远离支撑(乖离+8%) → 不算回调到位
    assert watch_dip_signal({**q, "price": 108.0}, tech, 107.6) is None
    # 无5分钟历史 → 不判
    assert watch_dip_signal(q, tech, None) is None


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_realtime_change 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
