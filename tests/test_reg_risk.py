"""个股监管/停牌风险——连板异动核查派生器单测（纯函数·不连网）。

只覆盖确定性纯逻辑：anomaly_risk（连板→异常波动风险）。
suspended_codes/reg_news/reg_flag 依赖外部数据源(Tushare/博查)，此处不连网测试。
"""

from __future__ import annotations

from app.strategy.reg_risk import anomaly_risk


def test_none_below_threshold() -> None:
    # 普通股 <3连板 不构成异动风险
    assert anomaly_risk(0) is None
    assert anomaly_risk(None) is None
    assert anomaly_risk(2) is None


def test_warn_at_mid() -> None:
    # 普通股 3/4连板 = 已达异常波动(warn)
    for c in (3, 4):
        r = anomaly_risk(c)
        assert r is not None and r["level"] == "warn" and r["boards"] == c


def test_high_at_hi() -> None:
    # 普通股 ≥5连板 = 严重异常波动·核查风险高
    r = anomaly_risk(5)
    assert r is not None and r["level"] == "high" and r["boards"] == 5
    assert anomaly_risk(7)["level"] == "high"


def test_st_lower_thresholds() -> None:
    # ST 阈值更低：2连板=warn / 3连板=high
    assert anomaly_risk(1, is_st=True) is None
    assert anomaly_risk(2, is_st=True)["level"] == "warn"
    assert anomaly_risk(3, is_st=True)["level"] == "high"
    # 同样3连板：普通股仅 warn，ST 已 high（验证分档独立生效）
    assert anomaly_risk(3, is_st=False)["level"] == "warn"


def test_coerces_dirty_input() -> None:
    # 边界：非整型/字符串/浮点应被安全转换，不抛异常
    assert anomaly_risk("5")["level"] == "high"
    assert anomaly_risk(5.0)["level"] == "high"
    assert anomaly_risk("") is None


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_reg_risk 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
