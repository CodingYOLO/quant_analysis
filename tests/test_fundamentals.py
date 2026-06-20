"""
基本面/消息面速览 fundamentals 纯逻辑单测（去重/汇总/新闻质量过滤）。

零依赖，可直接运行：python -m tests.test_fundamentals
"""

from __future__ import annotations

from app.strategy.fundamentals import _fmt_period, _fina_summary, _is_quality


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


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
