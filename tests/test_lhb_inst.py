"""龙虎榜机构净买榜 lhb_inst 单测：机构席位聚合 + 科技过滤 + 买卖榜排序。

零网络，注入式 FakeProvider。直接运行：python -m tests.test_lhb_inst
"""

from __future__ import annotations

import pandas as pd

import app.strategy.lhb_inst as L


def _inst_df(rows: list[dict]) -> pd.DataFrame:
    cols = ["trade_date", "ts_code", "exalter", "buy", "sell", "net_buy", "side", "reason"]
    return pd.DataFrame(rows, columns=cols)


def _basic_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["ts_code", "name", "industry", "industry_l1"])


# ---- 1. 机构席位聚合 _agg_inst（只取机构专用·元→亿·多席位累加） ----

def test_agg_only_inst_seat() -> None:
    df = _inst_df([
        {"ts_code": "600000.SH", "exalter": "机构专用", "buy": 5e8, "sell": 2e8, "net_buy": 3e8, "reason": "涨幅偏离"},
        {"ts_code": "600000.SH", "exalter": "机构专用", "buy": 1e8, "sell": 0, "net_buy": 1e8, "reason": "涨幅偏离"},
        {"ts_code": "600000.SH", "exalter": "某游资", "buy": 9e8, "sell": 0, "net_buy": 9e8, "reason": "涨幅偏离"},
    ])
    agg = L._agg_inst(df)
    assert round(agg["600000.SH"]["net"], 2) == 4.0     # 3+1，游资不计
    assert round(agg["600000.SH"]["buy"], 2) == 6.0
    assert agg["600000.SH"]["seats"] == 2               # 两个机构席位


def test_agg_empty_safe() -> None:
    assert L._agg_inst(pd.DataFrame()) == {}
    assert L._agg_inst(None) == {}


# ---- 2. 科技判定 ----

def test_is_tech() -> None:
    assert L._is_tech("电子") and L._is_tech("计算机") and L._is_tech("通信")
    assert not L._is_tech("食品饮料") and not L._is_tech("")


# ---- 3. 端到端 build_inst_board（注入式 FakeProvider） ----

class _FakeProvider:
    def __init__(self, inst_df, basic_df):
        self._inst, self._basic = inst_df, basic_df

    def get_lhb_inst(self, trade_date):
        return self._inst

    def get_stock_basic(self):
        return self._basic


def _provider():
    inst = _inst_df([
        {"ts_code": "603986.SH", "exalter": "机构专用", "buy": 6e8, "sell": 1e8, "net_buy": 5e8, "reason": "涨停"},   # 半导体·净买5亿
        {"ts_code": "002297.SZ", "exalter": "机构专用", "buy": 1e8, "sell": 13e8, "net_buy": -12e8, "reason": "换手"},  # 机械·净卖12亿
        {"ts_code": "600519.SH", "exalter": "机构专用", "buy": 3e8, "sell": 1e8, "net_buy": 2e8, "reason": "偏离"},   # 食品饮料·净买2亿
    ])
    basic = _basic_df([
        {"ts_code": "603986.SH", "name": "兆易创新", "industry": "半导体", "industry_l1": "电子"},
        {"ts_code": "002297.SZ", "name": "博实股份", "industry": "自动化设备", "industry_l1": "机械设备"},
        {"ts_code": "600519.SH", "name": "贵州茅台", "industry": "白酒", "industry_l1": "食品饮料"},
    ])
    return _FakeProvider(inst, basic)


def test_board_buy_sell_sort_and_names() -> None:
    b = L.build_inst_board(_provider(), "20260618", top=10)
    assert b["n_total"] == 3
    # 买榜按净买降序：茅台(2亿)在内，兆易(5亿)居首
    assert b["buys"][0]["name"] == "兆易创新" and b["buys"][0]["net_yi"] == 5.0
    assert b["buys"][0]["industry"] == "半导体" and b["buys"][0]["is_tech"]
    # 卖榜按净卖升序：博实(-12亿)
    assert b["sells"][0]["name"] == "博实股份" and b["sells"][0]["net_yi"] == -12.0


def test_board_tech_only_filter() -> None:
    b = L.build_inst_board(_provider(), "20260618", top=10, tech_only=True)
    names = {f["name"] for f in b["buys"] + b["sells"]}
    assert "兆易创新" in names and "博实股份" in names   # 电子/机械设备=科技
    assert "贵州茅台" not in names                        # 食品饮料被过滤
    assert b["n_total"] == 2


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_lhb_inst 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
