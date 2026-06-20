"""
基本面/消息面速览 fundamentals 纯逻辑单测（去重/汇总/新闻质量过滤）。

零依赖，可直接运行：python -m tests.test_fundamentals
"""

from __future__ import annotations

import pandas as pd

import datetime

from app.strategy.fundamentals import (
    _analyst_summary, _events_summary, _express_summary, _fina_summary, _float_summary,
    _fmt_period, _holder_trade_summary, _holdernum_summary, _is_quality, _latest_forecast,
    _survey_summary, get_analyst_rc,
)


def _d(days_ago: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days_ago)).strftime("%Y%m%d")


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
    def __init__(self, rows=None, survey_rows=None, rc_rows=None, rc_exc=False):
        self._rows = rows or []
        self._sv, self._rc, self._rc_exc = survey_rows, rc_rows, rc_exc
    def get_forecast(self, ts_code):
        return pd.DataFrame(self._rows)
    def get_survey(self, ts_code):
        return pd.DataFrame(self._sv) if self._sv is not None else pd.DataFrame()
    def get_report_rc(self, ts_code):
        if self._rc_exc:
            raise Exception("您访问接口(report_rc)频率超限(1次/小时)")
        return pd.DataFrame(self._rc) if self._rc is not None else pd.DataFrame()

    # 事件面（默认空，测试时按需注入）
    _ev: dict = {}

    def get_share_float(self, c):
        return self._ev.get("float", pd.DataFrame())

    def get_holder_trade(self, c):
        return self._ev.get("trade", pd.DataFrame())

    def get_express(self, c):
        return self._ev.get("express", pd.DataFrame())

    def get_holder_number(self, c):
        return self._ev.get("num", pd.DataFrame())


def test_float_summary() -> None:
    fut, past = _d(-20), _d(10)   # 20天后解禁、10天前已解禁
    df = pd.DataFrame({"float_date": [fut, fut, past], "float_ratio": [1.5, 0.5, 3.0]})
    s = _float_summary(df)
    assert s["next_ratio"] == 2.0 and s["upcoming_count"] == 1   # 同日聚合1.5+0.5；过去的不算
    assert 18 <= s["next_days"] <= 21
    assert _float_summary(pd.DataFrame()) is None


def test_holder_trade_summary() -> None:
    df = pd.DataFrame({"ann_date": ["20260601", "20260610", "20260605"], "in_de": ["DE", "IN", "DE"],
                       "change_ratio": [1.2, 0.5, 2.0], "holder_name": ["张三", "李四", "王五"]})
    s = _holder_trade_summary(df)
    assert s["de_count"] == 2 and s["in_count"] == 1
    assert s["latest"]["date"] == "2026-06-10" and s["latest"]["type"] == "增持"   # 取最新公告


def test_express_summary() -> None:
    # yoy_net_profit=去年同期净利(1亿)，本期净利2亿 → 同比+100%
    df = pd.DataFrame({"ann_date": ["20260415"], "end_date": ["20260331"], "revenue": [1.2e9],
                       "n_income": [2.0e8], "yoy_net_profit": [1.0e8], "diluted_roe": [8.1]})
    s = _express_summary(df)
    assert s["period"] == "2026一季报" and s["revenue_yi"] == 12.0
    assert s["net_profit_yi"] == 2.0 and s["net_profit_yoy"] == 100.0


def test_holdernum_summary() -> None:
    df = pd.DataFrame({"end_date": ["20260331", "20251231"], "ann_date": ["x", "y"],
                       "holder_num": [45000, 50000]})
    s = _holdernum_summary(df)
    assert s["latest"] == 45000 and s["chg_pct"] == -10.0 and "集中" in s["trend"]


def test_events_summary_bundles_present_only() -> None:
    fp = _FakeProvider()
    fp._ev = {
        "float": pd.DataFrame({"float_date": [_d(-20)], "float_ratio": [2.0]}),
        "num": pd.DataFrame({"end_date": ["20260331", "20251231"], "ann_date": ["x", "y"],
                             "holder_num": [40000, 50000]}),
        # trade/express 留空 → 不应出现在结果
    }
    ev = _events_summary("x", fp)
    assert "float" in ev and "holdernum" in ev
    assert "holder_trade" not in ev and "express" not in ev
    assert _events_summary("x", _FakeProvider()) is None     # 全空


def test_survey_summary() -> None:
    rows = [{"surv_date": _d(10), "rece_mode": "电话会议"}, {"surv_date": _d(40), "rece_mode": "现场调研"},
            {"surv_date": _d(80), "rece_mode": "业绩说明会"}, {"surv_date": _d(200), "rece_mode": "旧"}]
    s = _survey_summary("x", _FakeProvider(survey_rows=rows))
    assert s["count_90d"] == 3 and s["count_180d"] == 3 and s["heat"] == "中"
    assert s["recent"][0]["mode"] == "电话会议"             # 最新在前
    assert _survey_summary("x", _FakeProvider(survey_rows=[])) is None


def test_analyst_summary() -> None:
    df = pd.DataFrame([
        {"org_name": "中信", "rating": "买入", "max_price": 60, "min_price": 50, "report_date": "20260601"},
        {"org_name": "中金", "rating": "增持", "max_price": 55, "min_price": 45, "report_date": "20260610"},
        {"org_name": "中信", "rating": "买入", "max_price": 58, "min_price": 52, "report_date": "20260605"},
    ])
    a = _analyst_summary(df)
    assert a["ok"] and a["n_reports"] == 3 and a["n_org"] == 2     # 两家机构
    assert a["target_low"] == 45 and a["target_high"] == 60
    assert a["target_avg"] == 53.33                                # 各研报中点均值
    assert a["ratings"]["买入"] == 2 and a["latest"] == "2026-06-10"


def test_get_analyst_rc_graceful() -> None:
    assert get_analyst_rc("x", _FakeProvider(rc_exc=True))["ok"] is False   # 限频→优雅降级不抛
    assert get_analyst_rc("x", _FakeProvider(rc_rows=[]))["ok"] is False    # 无券商覆盖
    ok = get_analyst_rc("x", _FakeProvider(rc_rows=[
        {"org_name": "A", "rating": "买入", "max_price": 10, "min_price": 8, "report_date": "20260601"}]))
    assert ok["ok"] and ok["n_org"] == 1


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


def test_forecast_stale_filtered() -> None:
    # 数年前的旧预告（公告日超过~18个月）→ 视为过期，不展示
    old = _FakeProvider([{"ann_date": "20211217", "end_date": "20211231", "type": "略减",
                          "p_change_min": -40, "p_change_max": -30, "summary": "x"}])
    assert _latest_forecast("x", old) is None
    # 近期预告 → 正常返回
    fresh = _FakeProvider([{"ann_date": _d(30), "end_date": "20260331", "type": "预增",
                            "p_change_min": 50, "p_change_max": 60, "summary": "y"}])
    assert _latest_forecast("x", fresh)["type"] == "预增"


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
