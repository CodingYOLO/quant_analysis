"""
金牌分析师 analysts 纯函数单测（科技行业过滤 / 收益率列识别 / 当前持有优先）。

零依赖，可直接运行：python -m tests.test_analysts
"""

from __future__ import annotations

import pandas as pd

import app.strategy.analysts as A


def test_board_rows_tech_filter() -> None:
    df = pd.DataFrame([
        {"序号": 1, "分析师名称": "甲", "分析师单位": "X", "2025年收益率": 120, "3个月收益率": 10,
         "6个月收益率": 50, "12个月收益率": 120, "成分股个数": 5,
         "2025最新个股评级-股票名称": "北方华创", "2025最新个股评级-股票代码": "002371",
         "分析师ID": "a1", "行业": "电子"},
        {"序号": 2, "分析师名称": "乙", "分析师单位": "Y", "2025年收益率": 110, "3个月收益率": 8,
         "6个月收益率": 40, "12个月收益率": 110, "成分股个数": 3,
         "2025最新个股评级-股票名称": "贵州茅台", "2025最新个股评级-股票代码": "600519",
         "分析师ID": "a2", "行业": "食品饮料"},
    ])
    tech = A._board_rows(df, tech_only=True, top=10)
    assert [r["name"] for r in tech] == ["甲"]                 # 食品饮料被过滤
    assert tech[0]["ret_year"] == 120 and tech[0]["pick"] == "北方华创" and tech[0]["analyst_id"] == "a1"
    assert len(A._board_rows(df, tech_only=False, top=10)) == 2


def test_picks_held_priority() -> None:
    df = pd.DataFrame([
        {"股票代码": "600519", "股票名称": "茅台", "调入日期": "2024-01-01", "调出日期": "2024-06-01", "调入时评级名称": "买入"},
        {"股票代码": "002371", "股票名称": "北方华创", "调入日期": "2025-09-01", "调出日期": "", "调入时评级名称": "增持"},
        {"股票代码": "688981", "股票名称": "中芯", "调入日期": "2025-03-01", "调出日期": "-", "调入时评级名称": "买入"},
    ])
    p = A._picks(df, top=10)
    assert p["ok"] and p["n"] == 3 and p["held"] == 2          # 调出为空/"-" 视为当前持有
    assert p["items"][0]["held"] and p["items"][0]["name"] == "北方华创"   # 持有优先+调入新→旧
    assert p["items"][-1]["name"] == "茅台" and p["items"][-1]["out_date"] == "2024-06-01"


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
