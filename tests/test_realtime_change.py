"""盘中"变化"追踪：板块加速/轮动 + 大盘趋势 + 市场快照 + 重大变化（纯函数·不连网）。"""

from __future__ import annotations

from app.strategy.realtime_fund import (_accel_tag, breadth_trend, detect_market_shifts,
                                        market_pulse_text, sector_flow_delta)


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


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_realtime_change 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
