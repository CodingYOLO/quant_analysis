"""
同类/板块回测 sector_backtest 单测（纯函数 + 同类解析，零网络）。

可直接运行：python -m tests.test_sector_backtest
"""

from __future__ import annotations

import pandas as pd

import app.backtest.sector_backtest as S


def _k(dates: list[str], closes: list[float]) -> pd.DataFrame:
    """构造最简单股日线（open=close，便于断言）。"""
    return pd.DataFrame({"trade_date": dates, "open": closes, "high": closes,
                         "low": closes, "close": closes, "vol": [1] * len(closes),
                         "amount": [1.0] * len(closes), "pct_chg": [0.0] * len(closes)})


def test_breadth_band_boundaries() -> None:
    assert S._breadth_band(70) == "板块强(≥60%)"
    assert S._breadth_band(60) == "板块强(≥60%)"        # 边界含
    assert S._breadth_band(50) == "板块中性(40-60%)"
    assert S._breadth_band(40) == "板块中性(40-60%)"     # 边界含
    assert S._breadth_band(20) == "板块弱(<40%)"


def test_sector_breadth() -> None:
    dates = [f"d{i:03d}" for i in range(25)]
    up = list(range(100, 125))            # 上行 → 末端站上 MA20
    down = list(range(125, 100, -1))      # 下行 → 末端跌破 MA20
    br = S.sector_breadth({"A": _k(dates, up), "B": _k(dates, down)})
    last = br.iloc[-1]
    assert last["pct_ma20"] == 50.0       # 一上一下 → 50%
    assert S.sector_breadth({}).empty     # 空安全


def test_occurrences_no_future_leak() -> None:
    dates = [f"d{i:03d}" for i in range(20)]
    k = _k(dates, [100 + i for i in range(20)])         # 持续上行
    sd = {"min_bars": 3, "detect": lambda o: True, "label": "恒真"}
    occ = S._occurrences(k, sd, start="d000")
    assert len(occ) > 0
    assert "date" in occ[0] and occ[0]["rets"].get(3, 0) > 0    # 上行 → T+3 为正
    # start 过滤生效
    assert all(o["date"] >= "d010" for o in S._occurrences(k, sd, start="d010"))


def test_pool_stats_buckets_by_breadth() -> None:
    dates = [f"d{i:03d}" for i in range(20)]
    series = {"A": _k(dates, [100 + i for i in range(20)])}   # 上行
    sd = {"min_bars": 3, "detect": lambda o: True, "label": "x"}
    breadth_map = {d: 70 for d in dates}                     # 全程板块强
    st = S._pool_stats(series, sd, "d000", breadth_map)
    assert st["n_occ"] > 0
    assert st["pooled"][3]["n"] > 0
    assert "板块强(≥60%)" in st["by_breadth"]               # 信号落入板块强桶
    assert "板块弱(<40%)" not in st["by_breadth"]


class _FakeProv:
    """同类解析所需最小桩：stock_basic + trade_cal + daily_basic。"""
    def __init__(self):
        self.sb = pd.DataFrame({
            "ts_code": ["600519.SH", "000858.SZ", "000568.SZ", "600809.SH", "STX.SZ", "000001.SZ"],
            "name": ["贵州茅台", "五粮液", "泸州老窖", "山西汾酒", "ST酒鬼", "平安银行"],
            "industry": ["白酒", "白酒", "白酒", "白酒", "白酒", "银行"],
        })

    def get_stock_basic(self):
        return self.sb

    def get_trade_cal(self, s, e):
        return pd.DataFrame({"cal_date": [e], "is_open": [1]})

    def get_daily_basic(self, d):
        return pd.DataFrame({"ts_code": ["000858.SZ", "000568.SZ", "600809.SH"],
                             "circ_mv": [3000.0, 2000.0, 1000.0]})


def test_resolve_peers() -> None:
    ind, peers = S._resolve_peers(_FakeProv(), "600519.SH", "20240101", max_peers=10)
    codes = [c for c, _n in peers]
    assert ind == "白酒"
    assert "600519.SH" not in codes        # 剔除自身
    assert "STX.SZ" not in codes           # 剔除 ST
    assert "000001.SZ" not in codes        # 剔除非同行业（银行）
    assert codes[0] == "000858.SZ"         # 按市值降序，最大在前


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
