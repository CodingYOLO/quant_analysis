"""
基本面/消息面速览 fundamentals 纯逻辑单测（去重/汇总/新闻质量过滤）。

零依赖，可直接运行：python -m tests.test_fundamentals
"""

from __future__ import annotations

import pandas as pd

from app.strategy.fundamentals import _fmt_period, _fina_summary, _is_quality, _latest_forecast


def test_fmt_period() -> None:
    assert _fmt_period("20251231") == "2025年报"
    assert _fmt_period("20250930") == "2025三季报"
    assert _fmt_period("20250630") == "2025中报"
    assert _fmt_period("20260331") == "2026一季报"


def test_fina_summary() -> None:
    s = _fina_summary([{"period": "2026一季报", "netprofit_yoy": 62.9, "or_yoy": 53.9,
                        "debt_to_assets": 48.6, "grossprofit_margin": 35.6, "roe": 7.8}])
    assert "高增长" in s and "健康" in s and "+62.9%" in s
    # 高负债 → 留意
    s2 = _fina_summary([{"period": "x", "netprofit_yoy": -10, "or_yoy": 0,
                         "debt_to_assets": 72, "grossprofit_margin": 10, "roe": 1}])
    assert "下滑" in s2 and "偏高·留意" in s2
    assert _fina_summary([]) == ""


def test_news_quality_filter() -> None:
    # 行情快照标题 → 剔除
    assert _is_quality({"title": "沪电股份:133.36 8.83% +10.82 002463 搜狐证券", "site": "搜狐股票"}) is False
    assert _is_quality({"title": "某股最新价格_行情_走势图", "site": "东方财富网"}) is False
    assert _is_quality({"title": "诊股报告", "site": "牛炒股"}) is False
    # 实质性新闻 → 保留
    assert _is_quality({"title": "沪电股份:2026年6月16日投资者关系活动记录表", "site": "中国财经信息网"}) is True
    assert _is_quality({"title": "沪电股份扩产旨在提前卡位优质产能", "site": "财闻网"}) is True


class _FakeProvider:
    def __init__(self, rows):
        self._rows = rows
    def get_forecast(self, ts_code):
        return pd.DataFrame(self._rows)


def test_forecast_classification() -> None:
    def fc(t, lo=None, hi=None):
        p = _FakeProvider([{"ann_date": "20260414", "end_date": "20260331",
                            "type": t, "p_change_min": lo, "p_change_max": hi, "summary": "x"}])
        return _latest_forecast("002463.SZ", p)

    assert fc("预增", 54.76, 65.25)["level"] == "good"
    assert fc("预增", 54.76, 65.25)["net_change"] == "+55~+65%"
    assert fc("预亏")["level"] == "bad" and fc("预亏")["net_change"] is None
    assert fc("扭亏")["level"] == "good"          # 扭亏=利好
    assert fc("增亏", -50, -40)["level"] == "bad"   # 增亏=亏损扩大=利空
    assert fc("减亏", -10, -5)["level"] == "good"   # 减亏=亏损收窄=向好
    # 取最新公告日
    p = _FakeProvider([
        {"ann_date": "20260101", "end_date": "20251231", "type": "预减", "p_change_min": -20, "p_change_max": -10, "summary": "旧"},
        {"ann_date": "20260414", "end_date": "20260331", "type": "预增", "p_change_min": 50, "p_change_max": 60, "summary": "新"},
    ])
    assert _latest_forecast("x", p)["type"] == "预增"   # 取 ann_date 最新


def test_forecast_none_safe() -> None:
    assert _latest_forecast("x", _FakeProvider([])) is None


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
