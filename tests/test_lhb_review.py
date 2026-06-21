"""个股龙虎榜复盘 lhb_review 单测：异动日过滤 / T+N走势 / 分类 / 规律聚合 / 集成。

零网络（注入 FakeProvider）。直接运行：python -m tests.test_lhb_review
"""

from __future__ import annotations

import pandas as pd

import app.strategy.lhb_review as R


def _kline(rows):
    return pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"])


# ---- 1. 异动候选日 ----

def test_candidate_days() -> None:
    k = _kline([
        ["20260601", 10, 10.3, 9.9, 10.1, 1, 1, 1.0],     # 平静
        ["20260602", 10.1, 11.2, 10.0, 11.1, 1, 1, 9.9],  # 涨停·异动
        ["20260603", 11.1, 11.3, 11.0, 11.2, 1, 1, 0.9],  # 平静
        ["20260604", 11.2, 13.0, 10.8, 11.0, 1, 1, -1.8], # 振幅(13-10.8)/11.2=19.6%·异动
    ])
    days = R._candidate_days(k)
    assert "20260602" in days and "20260604" in days and "20260601" not in days


# ---- 2. T+N 走势 ----

def test_fwd_returns() -> None:
    k = _kline([["20260601", 10, 10, 10, 10, 1, 1, 0],
                ["20260602", 10, 10, 10, 11, 1, 1, 10],   # day
                ["20260603", 11, 11, 11, 12.1, 1, 1, 10], # T+1 vs day(11): +10%
                ["20260604", 12, 12, 12, 9.9, 1, 1, -18]])# T+2
    r = R._fwd_returns(k, "20260602", [1, 3])
    assert r[1] == 10.0          # 12.1/11-1
    assert r[3] is None          # T+3 越界·未到期


# ---- 3. 分类 ----

def test_category() -> None:
    assert R._category(1.0, 0.1, 0.0) == "机构净买"
    assert R._category(-0.5, 0.0, 0.0) == "机构出货"
    assert R._category(0.1, 1.0, 0.0) == "游资主导"
    assert R._category(0.0, 0.0, 0.5) == "北向加仓"
    assert R._category(0.0, 0.0, 0.0) == "分歧/其他"


# ---- 4. 规律聚合 ----

def test_pattern() -> None:
    occ = [
        {"category": "机构净买", "t1": 2.0, "t5": 3.0},
        {"category": "机构净买", "t1": 1.0, "t5": -1.0},
        {"category": "游资主导", "t1": -0.5, "t5": None},
    ]
    p = R._pattern(occ)
    inst = next(r for r in p if r["category"] == "机构净买")
    assert inst["count"] == 2 and inst["avg_t1"] == 1.5 and inst["avg_t5"] == 1.0 and inst["win_t5"] == 50
    assert p[0]["category"] == "机构净买"   # 按次数降序


# ---- 5. 集成（注入 FakeProvider） ----

class _Fake:
    def __init__(self, k, lhb_by_date):
        self._k = k; self._lhb = lhb_by_date

    def get_lhb_inst(self, d):
        return self._lhb.get(d, pd.DataFrame())


def test_review_stock_integration(monkeypatch=None) -> None:
    k = _kline([
        ["20260601", 10, 10, 10, 10, 1, 1, 0],
        ["20260602", 10, 11.2, 10, 11, 1, 1, 10],     # 异动·上榜
        ["20260603", 11, 11, 11, 11.5, 1, 1, 4.5],
        ["20260604", 11.5, 11.5, 11.5, 12.1, 1, 1, 5],  # T+2 of 0602: 12.1/11-1=+10%
    ])
    lhb = {"20260602": pd.DataFrame([
        {"ts_code": "300X.SZ", "exalter": "机构专用", "buy": 5e8, "sell": 1e8, "net_buy": 4e8, "reason": "涨幅偏离"},
        {"ts_code": "300X.SZ", "exalter": "机构专用", "buy": 3e8, "sell": 1e8, "net_buy": 2e8, "reason": "涨幅偏离"},
    ], columns=["ts_code", "exalter", "buy", "sell", "net_buy", "reason"])}
    # 注入 load_kline
    R.load_kline = lambda *a, **kw: k
    out = R.review_stock(_Fake(k, lhb), "300X.SZ", "20260601", "20260604")
    assert out["ok"] and out["count"] == 1
    o = out["occurrences"][0]
    assert o["date"] == "20260602" and o["category"] == "机构净买"
    assert o["t1"] is not None
    assert out["pattern"][0]["category"] == "机构净买"


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_lhb_review 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
