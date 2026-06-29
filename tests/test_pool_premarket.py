"""盘前·选股池消息面体检——确定性事件分类器单测（纯函数·不连网）。"""

from __future__ import annotations

from app.strategy.pool_premarket import _classify_events, _reg_for


def test_empty_is_neutral() -> None:
    r = _classify_events(None, None)
    assert r["verdict"] == "中性" and r["ups"] == [] and r["downs"] == []
    assert _classify_events({}, {})["verdict"] == "中性"


def test_forecast_good_bad() -> None:
    assert _classify_events(None, {"type": "预增", "level": "good", "net_change": "+50%"})["verdict"] == "利好"
    assert _classify_events(None, {"type": "首亏", "level": "bad"})["verdict"] == "利空"
    # neutral 档不计入多空
    assert _classify_events(None, {"type": "略增", "level": "neutral"})["verdict"] == "中性"


def test_express_yoy_sign() -> None:
    assert _classify_events({"express": {"net_profit_yoy": 120.0}}, None)["verdict"] == "利好"
    assert _classify_events({"express": {"net_profit_yoy": -45.0}}, None)["verdict"] == "利空"


def test_holder_trade_net() -> None:
    assert _classify_events({"holder_trade": {"de_count": 3, "in_count": 0}}, None)["verdict"] == "利空"
    assert _classify_events({"holder_trade": {"de_count": 0, "in_count": 2}}, None)["verdict"] == "利好"
    # 增减持次数相等 → 不形成方向
    assert _classify_events({"holder_trade": {"de_count": 1, "in_count": 1}}, None)["verdict"] == "中性"


def test_float_only_when_imminent() -> None:
    near = _classify_events({"float": {"next_days": 12, "next_ratio": 0.05}}, None)
    assert near["verdict"] == "利空" and "解禁" in near["downs"][0]["text"]
    far = _classify_events({"float": {"next_days": 90, "next_ratio": 0.05}}, None)
    assert far["verdict"] == "中性"        # 90天后解禁不计入抛压提醒


def test_block_premium_threshold() -> None:
    assert _classify_events({"block": {"premium_avg": -8.0}}, None)["verdict"] == "利空"
    assert _classify_events({"block": {"premium_avg": 6.0}}, None)["verdict"] == "利好"
    assert _classify_events({"block": {"premium_avg": -1.0}}, None)["verdict"] == "中性"   # 噪音过滤


def test_repurchase_only_when_real() -> None:
    assert _classify_events({"repurchase": {"is_real": True, "proc": "实施中", "amount_yi": 2.0}}, None)["verdict"] == "利好"
    assert _classify_events({"repurchase": {"is_real": False, "proc": "预案"}}, None)["verdict"] == "中性"


def test_mixed_signals() -> None:
    r = _classify_events(
        {"holder_trade": {"de_count": 2, "in_count": 0},
         "repurchase": {"is_real": True, "proc": "完成"}}, None)
    assert r["verdict"] == "混合" and r["downs"] and r["ups"]


def test_reg_for_suspend_priority() -> None:
    # 停牌(事实)优先于一切：即便连板也先报停牌
    r = _reg_for("000001.SZ", "平安银行", {"000001.SZ": {"consec_limit_now": 6}}, {"000001.SZ"})
    assert r is not None and r["kind"] == "suspend" and r["text"] == "停牌中"


def test_reg_for_anomaly_from_consec() -> None:
    # 未停牌·达连板阈值 → 异动核查
    r = _reg_for("300750.SZ", "宁德时代", {"300750.SZ": {"consec_limit_now": 5}}, set())
    assert r is not None and r["kind"] == "anomaly" and r["level"] == "high"


def test_reg_for_graceful_when_no_tech() -> None:
    # 盘前 hub 未加载(tech 为空)且未停牌 → 无标记(优雅降级·不报错)
    assert _reg_for("600000.SH", "浦发银行", {}, set()) is None


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_pool_premarket 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
