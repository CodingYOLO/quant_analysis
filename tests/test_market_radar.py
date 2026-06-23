"""全市场异动雷达单测：聚合纯函数(热点板块/涨跌榜/涨停/涨跌家数)·零网络。"""

from __future__ import annotations

import pandas as pd

import app.strategy.market_radar as MR


def _limit(code, name):           # 主板10%/创业科创20%/北交30%(简化)
    c = code[:3]
    if c in ("688", "689") or c in ("300", "301"):
        return 20.0
    if code[0] in ("8", "4"):
        return 30.0
    return 10.0


def _df():
    return pd.DataFrame([
        {"ts_code": "600001.SH", "name": "甲A", "price": 11.0, "pct_chg": 9.95},   # 主板涨停
        {"ts_code": "600002.SH", "name": "甲B", "price": 10.5, "pct_chg": 3.0},
        {"ts_code": "600003.SH", "name": "甲C", "price": 10.0, "pct_chg": 1.0},
        {"ts_code": "000010.SZ", "name": "乙A", "price": 9.0, "pct_chg": -8.0},
        {"ts_code": "000011.SZ", "name": "乙B", "price": 9.0, "pct_chg": -9.97},     # 主板跌停
        {"ts_code": "000012.SZ", "name": "乙C", "price": 9.0, "pct_chg": -5.0},
        {"ts_code": "300001.SZ", "name": "ST丙", "price": 5.0, "pct_chg": 19.9},     # 创业涨停·ST
    ])


def _ind():
    return {"600001.SH": "甲行业", "600002.SH": "甲行业", "600003.SH": "甲行业",
            "000010.SZ": "乙行业", "000011.SZ": "乙行业", "000012.SZ": "乙行业",
            "300001.SZ": "丙行业"}   # 丙只1只·应被min_n剔除


def test_hot_sectors_ranked_with_leader() -> None:
    r = MR._aggregate_radar(_df(), _ind(), _limit)
    hot = r["hot_sectors"]
    assert [h["industry"] for h in hot] == ["甲行业", "乙行业"]   # 甲(均+4.65)在乙(均-7.66)前·丙剔除
    assert hot[0]["leader"] == "甲A" and hot[0]["limit_up"] == 1


def test_breadth_and_limits() -> None:
    r = MR._aggregate_radar(_df(), _ind(), _limit)
    b = r["breadth"]
    assert b["total"] == 7 and b["up"] == 4 and b["down"] == 3
    assert b["limit_up"] == 2 and b["limit_down"] == 1            # 甲A + ST丙 涨停;乙B跌停
    assert any(x["name"] == "ST丙" and x["is_st"] for x in r["limit_ups"])


def test_gainers_losers_sorted() -> None:
    r = MR._aggregate_radar(_df(), _ind(), _limit)
    assert r["gainers"][0]["name"] == "ST丙"                      # 涨幅最高(+19.9·ST也算异动·已标记)
    assert r["losers"][0]["name"] == "乙B"                        # 跌幅最深(-9.97)


def test_empty_safe() -> None:
    r = MR._aggregate_radar(pd.DataFrame(), {}, _limit)
    assert r["hot_sectors"] == [] and r["breadth"] == {}


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_market_radar 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
