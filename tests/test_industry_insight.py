"""产业认知：个股清单提取 + LLM JSON 数组鲁棒解析单测(纯函数·零网络)。"""

from __future__ import annotations

import pandas as pd

import app.strategy.industry_insight as II


def _sample_df() -> pd.DataFrame:
    """构造一个含龙头/高成长/ST 的小样本，覆盖各分支。"""
    return pd.DataFrame([
        dict(name="龙头甲", ts_code="000001.SZ", industry="半导体", netprofit_yoy=45.0,
             forecast_type="预增", forecast_chg=80.0, earn_good=True, circ_mv_100m=320.0,
             rps120=92.0, is_leader=True, is_st=False, leader_score=0.95),
        dict(name="成长丙", ts_code="000003.SZ", industry="半导体", netprofit_yoy=520.0,
             forecast_type="扭亏", forecast_chg=300.0, earn_good=True, circ_mv_100m=45.0,
             rps120=70.0, is_leader=False, is_st=False, leader_score=0.40),
        dict(name="亏损丁", ts_code="000004.SZ", industry="半导体", netprofit_yoy=-30.0,
             forecast_type="", forecast_chg=None, earn_good=False, circ_mv_100m=60.0,
             rps120=30.0, is_leader=False, is_st=False, leader_score=0.20),
        dict(name="ST戊", ts_code="000005.SZ", industry="半导体", netprofit_yoy=900.0,
             forecast_type="扭亏", forecast_chg=999.0, earn_good=True, circ_mv_100m=18.0,
             rps120=50.0, is_leader=False, is_st=True, leader_score=0.10),
    ])


def test_brief_rows_clean_ints() -> None:
    """市值/RPS 取整、龙头标记、字段齐全。"""
    g = _sample_df()
    rows = II._brief_rows(g.sort_values("leader_score", ascending=False), 2)
    assert rows[0]["name"] == "龙头甲" and rows[0]["is_leader"] is True
    assert rows[0]["流通市值"] == 320 and isinstance(rows[0]["流通市值"], int)
    assert rows[0]["rps"] == 92 and rows[0]["code"] == "000001"


def test_growth_pool_excludes_st_and_loss() -> None:
    """高成长池：剔除 ST 与负增长，按净利同比降序。"""
    names = [r["name"] for r in II._brief_rows(II._growth_pool(_sample_df()), 9)]
    assert "ST戊" not in names and "亏损丁" not in names
    assert names == ["成长丙", "龙头甲"]            # 520% 在前


def test_catalyst_pool_excludes_st() -> None:
    """催化池：业绩预喜但剔除 ST，按预告增幅降序。"""
    names = [r["name"] for r in II._brief_rows(II._catalyst_pool(_sample_df()), 9)]
    assert "ST戊" not in names
    assert names == ["成长丙", "龙头甲"]            # 预告 300% 在前


def test_stock_line_format() -> None:
    """个股一行式：含市值/同比/预告/RPS，无'增增'冗余。"""
    row = II._brief_rows(_sample_df().sort_values("leader_score", ascending=False), 1)[0]
    line = II._stock_line("龙头", row)
    assert "流通320亿" in line and "RPS92" in line and "预增+80" in line
    assert "增增" not in line


def test_parse_clean_array() -> None:
    qs = II._parse_json_array('[{"q":"题1","point":"考点1"},{"q":"题2","point":"考点2"}]')
    assert qs and len(qs) == 2 and qs[0]["q"] == "题1"


def test_parse_with_code_fence() -> None:
    raw = '```json\n[{"q":"a","point":"b"}]\n```'
    assert II._parse_json_array(raw) == [{"q": "a", "point": "b"}]


def test_parse_with_leading_prose() -> None:
    raw = '好的，以下是题目：\n[{"q":"x","point":"y"}]\n希望有帮助'
    assert II._parse_json_array(raw) == [{"q": "x", "point": "y"}]


def test_parse_bad_returns_none() -> None:
    assert II._parse_json_array("没有数组") is None
    assert II._parse_json_array("") is None
    assert II._parse_json_array("[坏json,]") is None


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_industry_insight 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
