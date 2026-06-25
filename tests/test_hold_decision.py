"""拿得住·卖出决策器单测（纯逻辑·零网络）。

重点验证手册铁律：**卖出判定只看趋势/纪律/量价，不看成本价**。
"""

from __future__ import annotations

from app.strategy.hold_decision import decide


def _row(**kw) -> dict:
    base = dict(ts_code="000001.SZ", name="测试", price=10.0, pnl=None, note="",
                stop_loss=None, above_ma20=True, above_ma60=True, ma20_up=True,
                ma20=9.5, volume_ratio=1.0)
    base.update(kw)
    return base


def test_below_stop_is_sell() -> None:
    d = decide(_row(price=9.0, stop_loss=9.5))
    assert d["level"] == "sell" and "止损" in d["verdict"]


def test_heavy_volume_breakdown_warns() -> None:
    d = decide(_row(above_ma20=False, volume_ratio=1.8))
    assert d["level"] == "warn" and "分批减" in d["verdict"]


def test_light_volume_pullback_is_watch() -> None:
    """缩量跌破MA20但守住MA60 → 观察(可能洗盘)，别恐慌卖。"""
    d = decide(_row(above_ma20=False, above_ma60=True, volume_ratio=0.6))
    assert d["level"] == "watch" and "洗盘" in d["verdict"]


def test_above_ma20_up_is_hold() -> None:
    d = decide(_row(above_ma20=True, ma20_up=True))
    assert d["level"] == "hold" and "持有" in d["verdict"]


def test_verdict_ignores_cost_huge_loss_still_hold() -> None:
    """铁律：浮亏 -40% 但趋势锚未破 → 仍持有，不因亏损而判卖（治锚定成本）。"""
    d = decide(_row(above_ma20=True, ma20_up=True, pnl=-40.0))
    assert d["level"] == "hold"


def test_verdict_ignores_cost_profit_but_broke_stop_sell() -> None:
    """铁律：浮盈 +30% 但已破止损 → 照样判止损，不因赚钱而留（治处置效应）。"""
    d = decide(_row(price=9.0, stop_loss=9.5, pnl=30.0))
    assert d["level"] == "sell"


def test_four_checks_present() -> None:
    d = decide(_row())
    assert len(d["checks"]) == 4
    assert d["checks"][0]["state"] == "ask"          # 逻辑=主观自查
    assert "成本价" in d["anchor_note"]


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_hold_decision 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
