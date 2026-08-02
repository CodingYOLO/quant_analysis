"""连续多年增长因子·纯函数测试（报告期选取 / 复合增速 / 逐年序列 / 亏损基期留空 / 缺数据留空）。

重点守住「数据准确性」纪律：基期亏损或年度缺失时**必须留空**，绝不给假增速。

运行：.venv/bin/python tests/test_multiyear_growth.py
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.screener import (  # noqa: E402
    _add_annual_roe, _add_dedt_growth_3y, _add_profitable_growth, _add_revenue_growth_3y,
    _annual_periods, _cagr_from_yoy, _fmt_growth_seq)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _close(a, b, tol: float = 0.15) -> bool:
    return a is not None and pd.notna(a) and abs(float(a) - b) <= tol


class FakeProvider:
    """按报告期返回预置财务表的假 provider（隔离网络·只测算法）。"""

    def __init__(self, by_period: dict[str, pd.DataFrame]) -> None:
        self._by_period = by_period

    def get_fina_indicator_by_period(self, period: str) -> pd.DataFrame:
        return self._by_period.get(period, pd.DataFrame())


# ── 报告期选取：年报要等披露齐备(次年5-15)，否则整体退一年 ────────────────────────
def test_annual_periods() -> None:
    _assert(_annual_periods(4, datetime.date(2026, 8, 2))
            == ["20251231", "20241231", "20231231", "20221231"], "8月应取2025及往前4个年度")
    _assert(_annual_periods(3, datetime.date(2026, 3, 1))
            == ["20241231", "20231231", "20221231"], "3月年报未披露齐·应退到2024")
    _assert(_annual_periods(1, datetime.date(2026, 5, 15))[0] == "20251231", "5-15当天应已可用2025")
    _assert(_annual_periods(1, datetime.date(2026, 5, 14))[0] == "20241231", "5-14仍应退一年")
    print("  ✓ 报告期选取：按5-15齐备线取年报·不齐则退一年")


# ── 复合增速：由逐年同比还原(∏(1+g))^(1/n)-1 ──────────────────────────────────
def test_cagr_from_yoy() -> None:
    g = pd.DataFrame([[20.0, 20.0, 20.0], [100.0, 0.0, 0.0], [-100.0, 50.0, 50.0]])
    out = _cagr_from_yoy(g)
    _assert(_close(out.iloc[0], 20.0), f"每年20%的复合应=20%，实得 {out.iloc[0]}")
    _assert(_close(out.iloc[1], 25.99), f"100/0/0 复合应≈26%，实得 {out.iloc[1]}")
    _assert(pd.isna(out.iloc[2]), "有一年-100%(归零)→乘积为0·复合无意义·应留空")
    print("  ✓ 复合增速：还原正确·归零年留空")


# ── 逐年序列：新→旧取整·缺失显 "-" ──────────────────────────────────────────
def test_fmt_growth_seq() -> None:
    g = pd.DataFrame([[38.4, 25.1, 22.9], [10.0, float("nan"), 5.0]])
    out = _fmt_growth_seq(g)
    _assert(out.iloc[0] == "38/25/23", f"应为 38/25/23，实得 {out.iloc[0]}")
    _assert(out.iloc[1] == "10/-/5", f"缺失年应显 '-'，实得 {out.iloc[1]}")
    print("  ✓ 逐年序列：新→旧取整·缺失显 '-'")


def _rev_provider() -> FakeProvider:
    """营收同比：A稳增·B有一年掉队·C低基数爆表·D缺一年。"""
    codes = ["A", "B", "C", "D"]
    return FakeProvider({
        "20251231": pd.DataFrame({"ts_code": codes, "or_yoy": [30.0, 30.0, 30.0, 30.0]}),
        "20241231": pd.DataFrame({"ts_code": codes, "or_yoy": [25.0, 5.0, 2000.0, 25.0]}),
        "20231231": pd.DataFrame({"ts_code": ["A", "B", "C"], "or_yoy": [22.0, 40.0, 10.0]}),
    })


def test_revenue_growth_3y() -> None:
    df = pd.DataFrame({"ts_code": ["A", "B", "C", "D"]})
    df = _add_revenue_growth_3y(df, _rev_provider(), ["20251231", "20241231", "20231231", "20221231"])
    r = df.set_index("ts_code")
    _assert(_close(r.loc["A", "or_yoy_3y_min"], 22.0), "A 三年最小值应=22")
    _assert(r.loc["A", "or_yoy_3y"] == "30/25/22", f"A 序列应 30/25/22，实得 {r.loc['A', 'or_yoy_3y']}")
    _assert(_close(r.loc["B", "or_yoy_3y_min"], 5.0), "B 有一年只有5%·最小值应=5(不达标可被筛掉)")
    _assert(pd.isna(r.loc["C", "or_cagr3"]), "C 有一年2000%(低基数失真)→复合增速应留空")
    _assert(_close(r.loc["C", "or_yoy_3y_min"], 10.0), "C 最小值不受爆表年影响·仍应给出")
    _assert(pd.isna(r.loc["D", "or_yoy_3y_min"]) and pd.isna(r.loc["D", "or_cagr3"]),
            "D 缺2023年报→整组必须留空(绝不用0填充)")
    print("  ✓ 营收3年：最小值/复合/序列正确·低基数不采信复合·缺年留空")


def _dedt_provider() -> FakeProvider:
    """扣非净利绝对值(元)：A四年均盈利稳增·B基期2022亏损·C缺2022·D持平·E单年爆表(低基数)。"""
    def frame(vals: dict[str, float]) -> pd.DataFrame:
        return pd.DataFrame({"ts_code": list(vals), "profit_dedt": list(vals.values())})
    return FakeProvider({
        "20251231": frame({"A": 2.197e8, "B": 3.0e8, "C": 3.0e8, "D": 1.0e8, "E": 5.2e8}),
        "20241231": frame({"A": 1.69e8, "B": 2.0e8, "C": 2.0e8, "D": 1.0e8, "E": 4.0e8}),
        "20231231": frame({"A": 1.3e8, "B": 1.0e8, "C": 1.0e8, "D": 1.0e8, "E": 2.0e6}),
        "20221231": frame({"A": 1.0e8, "B": -5.0e7, "D": 1.0e8, "E": 1.6e6}),
    })


def test_dedt_growth_3y() -> None:
    df = pd.DataFrame({"ts_code": ["A", "B", "C", "D", "E"]})
    df = _add_dedt_growth_3y(df, _dedt_provider(), ["20251231", "20241231", "20231231", "20221231"])
    r = df.set_index("ts_code")
    _assert(_close(r.loc["A", "dtnp_cagr3"], 30.0), f"A 每年30%·复合应=30%，实得 {r.loc['A', 'dtnp_cagr3']}")
    _assert(_close(r.loc["A", "dtnp_yoy_3y_min"], 30.0), "A 三年增速最小值应=30")
    _assert(r.loc["A", "dtnp_yoy_3y"] == "30/30/30", f"A 序列应 30/30/30，实得 {r.loc['A', 'dtnp_yoy_3y']}")
    _assert(pd.isna(r.loc["B", "dtnp_cagr3"]) and pd.isna(r.loc["B", "dtnp_yoy_3y_min"]),
            "⭐B 基期2022亏损·同比无意义(扭亏假增速)→必须整组留空")
    _assert(pd.isna(r.loc["C", "dtnp_cagr3"]), "C 缺2022年报→留空")
    _assert(_close(r.loc["D", "dtnp_cagr3"], 0.0), "D 四年持平·复合应=0(不是留空)")
    _assert(pd.isna(r.loc["E", "dtnp_cagr3"]), "⭐E 单年+19900%(低基数)→复合数会被那年主导·应留空")
    _assert(_close(r.loc["E", "dtnp_yoy_3y_min"], 25.0), "E 的最小值/序列不受爆表限制·仍应给出供用户自判")
    print("  ✓ 扣非3年：绝对值自算·亏损基期与缺年留空·爆表年只封复合不封逐年")


# ── 同比增速剔亏损股：Tushare 在亏损期照样给正同比("亏损收窄"≠成长) ──────────────
def test_profitable_growth_mask() -> None:
    df = pd.DataFrame({
        "ts_code": ["盈利成长", "亏损收窄", "亏损扩大", "无数据"],
        "profit_dedt": [2.0e8, -5.34e7, -8.6e9, float("nan")],   # 仿深振业A(-15.7亿→-0.53亿)
        "dt_netprofit_yoy": [35.0, 96.6, -89.3, 50.0],
        "netprofit_yoy": [33.0, 95.0, -88.0, 48.0],
    })
    out = _add_profitable_growth(df)
    r = out.set_index("ts_code")
    _assert(_close(r.loc["盈利成长", "dtnp_yoy_pos"], 35.0), "真盈利成长股应保留同比")
    _assert(bool(r.loc["盈利成长", "dedt_positive"]) is True, "扣非为正应标 True")
    _assert(pd.isna(r.loc["亏损收窄", "dtnp_yoy_pos"]),
            "⭐亏损收窄股 Tushare 给 +96.6%·必须屏蔽(否则会被『扣非同比≥20%』选出来)")
    _assert(pd.isna(r.loc["亏损收窄", "np_yoy_pos"]), "净利同比同样屏蔽")
    _assert(bool(r.loc["亏损收窄", "dedt_positive"]) is False, "仍亏损应标 False")
    _assert(pd.isna(r.loc["无数据", "dtnp_yoy_pos"]), "无扣非数据→留空(不放行)")
    _assert(_close(r.loc["盈利成长", "dt_netprofit_yoy"], 35.0)
            and _close(r.loc["亏损收窄", "dt_netprofit_yoy"], 96.6), "原始列必须原样保留(结果表透明展示)")
    print("  ✓ 剔亏损股：屏蔽『亏损收窄』的假正增速·原始列保持透明")


def test_profitable_growth_missing_cols() -> None:
    """取数失败(列全缺)时必须仍建列——缺列会让筛选 fail-open·放行未经基本面筛选的股。"""
    out = _add_profitable_growth(pd.DataFrame({"ts_code": ["A", "B"]}))
    for c in ("dedt_positive", "dtnp_yoy_pos", "np_yoy_pos"):
        _assert(c in out.columns, f"{c} 必须建列(哪怕全空)")
    _assert(out["dtnp_yoy_pos"].isna().all(), "无数据时应全空→筛不出(fail-closed)")
    _assert((~out["dedt_positive"]).all(), "无数据时不得标为盈利")
    print("  ✓ 取数失败：列仍建·全空 fail-closed·不放行")


# ── 年报 ROE：必须取**年报**期，不能用单季(单季比年度门槛=废掉因子) ────────────────
def test_annual_roe_uses_annual_period() -> None:
    periods = ["20251231", "20241231", "20231231", "20221231"]
    prov = FakeProvider({
        "20251231": pd.DataFrame({"ts_code": ["A", "B"], "roe": [18.5, 3.0]}),   # 年报口径
        "20241231": pd.DataFrame({"ts_code": ["A", "B"], "roe": [16.0, 2.0]}),
    })
    out = _add_annual_roe(pd.DataFrame({"ts_code": ["A", "B", "C"]}), prov, periods)
    r = out.set_index("ts_code")
    _assert(_close(r.loc["A", "roe_yr"], 18.5), "应取最近年报期的 ROE")
    _assert(_close(r.loc["B", "roe_yr"], 3.0), "低 ROE 也要如实给出")
    _assert(pd.isna(r.loc["C", "roe_yr"]), "无该股年报数据→留空")
    print("  ✓ 年报ROE：取年报期·缺数据留空")


if __name__ == "__main__":
    print("连续多年增长因子测试")
    for fn in (test_annual_periods, test_cagr_from_yoy, test_fmt_growth_seq,
               test_revenue_growth_3y, test_dedt_growth_3y,
               test_profitable_growth_mask, test_profitable_growth_missing_cols,
               test_annual_roe_uses_annual_period):
        fn()
    print("✅ 全部通过")
