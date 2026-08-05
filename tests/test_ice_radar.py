"""冰点雷达纯函数测试（2026-08-06）。"""

from __future__ import annotations

from app.strategy.ice_radar import EPISODES, ice_state


def test_double_by_pct():
    assert ice_state(10.0, 0.95, 12.0)["code"] == "double"


def test_double_by_ratio():
    # 口径D：缩量比≤0.78 + 情绪分位≤25(放宽)
    assert ice_state(30.0, 0.76, 22.0)["code"] == "double"


def test_vol_only():
    s = ice_state(10.0, 0.7, 88.0)
    assert s["code"] == "vol" and "情绪未冰" in s["detail"]


def test_senti_only():
    assert ice_state(60.0, 1.1, 10.0)["code"] == "senti"


def test_none_state():
    assert ice_state(50.0, 1.0, 50.0)["code"] == "none"


def test_na_state():
    assert ice_state(None, 1.0, 50.0)["code"] == "na"


def test_ratio_alone_not_double_when_senti_hot():
    # 20260804 实况：缩量比0.78·涨停分位88 → 只算量冰·不算双冰
    assert ice_state(25.0, 0.78, 88.0)["code"] == "vol"


def test_episodes_contain_loser():
    # 诚实红线：事件表必须含2023钝化亏损案例·不许只展示赢的
    assert any(e["t60"] < 0 for e in EPISODES)
    assert len(EPISODES) >= 8
