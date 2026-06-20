"""
股性画像 stock_profile 纯逻辑单测（标签/形态提示）。

零依赖，可直接运行：python -m tests.test_stock_profile
"""

from __future__ import annotations

import pandas as pd

from app.strategy.stock_profile import _tags, _form_hints, _kline_payload, _chip_tags


def _base_metrics(**kw) -> dict:
    m = {"amplitude_avg": 4.0, "volatility_annual": 30.0, "limit_up_1y": 5,
         "limit_down_1y": 1, "max_board": 1, "above_ma20_ratio": 50.0,
         "max_drawdown": -30.0, "chase_nextday": 0.0, "up_day_ratio": 50.0,
         "avg_up": 2.0, "avg_down": -2.0}
    m.update(kw)
    return m


def test_tags_high_vol_and_spec() -> None:
    tags = _tags(_base_metrics(volatility_annual=60, limit_up_1y=20, max_board=4))
    texts = " ".join(t["text"] for t in tags)
    assert "高波动" in texts and "妖性" in texts
    assert any(t["level"] == "hot" for t in tags)


def test_tags_calm_trend() -> None:
    tags = _tags(_base_metrics(volatility_annual=20, above_ma20_ratio=65, chase_nextday=0.8))
    texts = " ".join(t["text"] for t in tags)
    assert "低波动" in texts and "趋势性强" in texts and "追高友好" in texts


def test_tags_chase_warn() -> None:
    tags = _tags(_base_metrics(chase_nextday=-0.9, max_drawdown=-55))
    texts = " ".join(t["text"] for t in tags)
    assert "追高谨慎" in texts and "回撤大" in texts


def _kline(closes, vols=None) -> pd.DataFrame:
    n = len(closes)
    vols = vols or [1000] * n
    return pd.DataFrame({
        "trade_date": [f"2026{(i % 12) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)],
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes, "vol": vols,
    })


def test_form_hints_bull_and_overheat() -> None:
    rising = [10 + i * 0.3 for i in range(70)]      # 持续上行 → 均线多头 + 站上MA20
    hints = " ".join(_form_hints(_kline(rising)))
    assert "均线多头排列" in hints and "站上MA20" in hints


def test_kline_payload_shape() -> None:
    kl = _kline_payload(_kline([10 + i * 0.1 for i in range(30)]))
    assert len(kl["candle"][0]) == 4 and len(kl["dates"]) == 30
    assert "ma5" in kl and "ma20" in kl and "ma60" in kl


def test_chip_tags() -> None:
    # 高溢价 + 高获利盘 + 集中 → 警示追高/抛压 + 集中
    tags = _chip_tags(premium=20, winner=94, concentration=12)
    texts = " ".join(t["text"] for t in tags)
    assert "追高风险" in texts and "高位抛压" in texts and "高度集中" in texts
    # 跌破成本 + 套牢重
    t2 = " ".join(t["text"] for t in _chip_tags(premium=-8, winner=20, concentration=45))
    assert "跌破主力成本" in t2 and "套牢盘重" in t2 and "分散" in t2
    # 缺数据安全
    assert _chip_tags(None, None, None) == []


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
