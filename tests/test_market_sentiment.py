"""
大盘情绪·官方连板序列 market_sentiment 单测（假 provider，零网络）。

可直接运行：python -m tests.test_market_sentiment
"""

from __future__ import annotations

import pandas as pd

import app.strategy.market_sentiment as MS


def _up_df(times: list[int]) -> pd.DataFrame:
    """构造官方涨停榜（limit_times=连板数）+ get_limit_analysis 需要的列。"""
    n = len(times)
    return pd.DataFrame({
        "ts_code": [f"00000{i}.SZ" for i in range(n)],
        "name": [f"票{i}" for i in range(n)],
        "limit_times": times,
        "fd_amount": [1e8] * n,
    })


class _FakeProv:
    def __init__(self, mapping):
        self.m = mapping                       # {(date, type): DataFrame}

    def get_limit_list(self, d, t):
        return self.m.get((d, t), pd.DataFrame())


def test_lianban_dist() -> None:
    dist, mx = MS._lianban_dist(_up_df([1, 1, 2, 2, 3, 5, 1]))
    assert dist == {2: 2, 3: 1, 5: 1} and mx == 5     # 1板不计入梯队；最高5板
    assert MS._lianban_dist(pd.DataFrame()) == ({}, 0)


def test_official_limit_series() -> None:
    dates = ["20260616", "20260617"]
    m = {
        ("20260616", "U"): _up_df([1, 2, 3]), ("20260616", "D"): _up_df([1]),
        ("20260617", "U"): _up_df([1, 1, 4, 4, 6]), ("20260617", "D"): pd.DataFrame(),
    }
    s = MS._official_limit_series(_FakeProv(m), dates)
    assert s is not None
    assert s["limit_up"] == [3, 5] and s["limit_down"] == [1, 0]   # 官方涨停/跌停家数
    lb = s["lianban"]
    assert lb["height"] == [3, 6]                                  # 最高连板 16日3板/17日6板
    assert lb["b3"] == [1, 0] and lb["b4"] == [0, 2] and lb["b5p"] == [0, 1]  # 17日:4板2家·6板入5板+
    assert MS._official_limit_series(_FakeProv(m), []) is None     # 空安全


def test_lhb_summary() -> None:
    dt = {
        "600111.SH": {"dominant": "游资", "net_buy_yi": 2.5, "seats": [{"tag": "🔥游资·赵老哥"}]},
        "000001.SZ": {"dominant": "机构", "net_buy_yi": 1.2, "seats": [{"tag": "机构专用"}]},
        "300750.SZ": {"dominant": "游资", "net_buy_yi": -0.8, "seats": [{"tag": "🔥游资·章盟主"}]},
    }
    c2n = {"600111.SH": "北方稀土", "000001.SZ": "平安银行", "300750.SZ": "宁德时代"}
    s = MS._lhb_summary(dt, c2n)
    assert s["n"] == 3
    assert s["dominant_dist"] == {"游资": 2, "机构": 1}        # 游资2家/机构1家
    assert s["famous"]["赵老哥"] == ["北方稀土"]                # 知名游资动向带股名
    assert s["top_net"][0]["name"] == "北方稀土" and s["top_net"][0]["net_yi"] == 2.5  # 净买额降序
    assert MS._lhb_summary({}, c2n) == {}                       # 空安全


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
