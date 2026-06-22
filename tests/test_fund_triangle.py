"""资金三角 fund_triangle 单测：纯分类规则 + 机构净买汇总 + 端到端集成。

零网络，依赖注入式 FakeProvider 打桩三接口。直接运行：
    python -m tests.test_fund_triangle
"""

from __future__ import annotations

import pandas as pd

import app.strategy.fund_triangle as FT


# ---------------------------------------------------------------------------
# 1. 纯分类规则 _classify（覆盖全部分支）
# ---------------------------------------------------------------------------

def test_classify_confirm() -> None:
    """主力流入 + 机构真买 → 真钱印证。"""
    label, level, _ = FT._classify(2.0, 1.0, on_lhb=True)
    assert label == FT._L_CONFIRM and level == "strong"


def test_classify_diverge() -> None:
    """主力流入 但 机构在龙虎榜净卖 → 背离警示。"""
    label, level, detail = FT._classify(2.0, -1.0, on_lhb=True)
    assert label == FT._L_DIVERGE and level == "warn"
    assert "背离" in detail


def test_classify_no_trace() -> None:
    """主力流入 但 无机构席位足迹 → 机构无足迹（诚实标注仅代理口径）。"""
    label, level, detail = FT._classify(2.0, 0.0, on_lhb=False)
    assert label == FT._L_NO_TRACE and level == "neutral"
    assert "无真钱印证" in detail


def test_classify_outflow() -> None:
    """主力净流出 → 资金流出（不看机构）。"""
    label, level, _ = FT._classify(-1.5, 5.0, on_lhb=True)
    assert label == FT._L_OUTFLOW and level == "weak"


def test_classify_neutral_inst_flat() -> None:
    """主力流入 + 机构持平 → 资金中性。"""
    label, level, _ = FT._classify(2.0, 0.0, on_lhb=True)
    assert label == FT._L_NEUTRAL and level == "neutral"


def test_classify_flat_main_inst_buy() -> None:
    """主力近似零 + 机构真买 → 仍判真钱印证。"""
    label, level, _ = FT._classify(0.0, 1.0, on_lhb=True)
    assert label == FT._L_CONFIRM and level == "strong"


# ---------------------------------------------------------------------------
# 2. 机构净买汇总 _inst_net_map（只取机构专用·元→亿·跨日累加）
# ---------------------------------------------------------------------------

def _lhb_inst_df(rows: list[dict]) -> pd.DataFrame:
    cols = ["trade_date", "ts_code", "exalter", "buy", "sell", "net_buy", "side", "reason"]
    return pd.DataFrame(rows, columns=cols)


class _InstProvider:
    """只为 _inst_net_map 打桩 get_lhb_inst。"""

    def __init__(self, by_date: dict[str, pd.DataFrame]):
        self._by_date = by_date

    def get_lhb_inst(self, trade_date: str) -> pd.DataFrame:
        return self._by_date.get(trade_date, pd.DataFrame())


def test_inst_net_map_filters_and_sums() -> None:
    """只汇总『机构专用』席位，游资席位剔除；元→亿；跨日累加。"""
    d1 = _lhb_inst_df([
        {"ts_code": "600000.SH", "exalter": "机构专用", "net_buy": 3e8},   # 3亿
        {"ts_code": "600000.SH", "exalter": "某游资营业部", "net_buy": 9e8},  # 应剔除
        {"ts_code": "000001.SZ", "exalter": "机构专用", "net_buy": -2e8},  # -2亿
    ])
    d2 = _lhb_inst_df([
        {"ts_code": "600000.SH", "exalter": "机构专用", "net_buy": 1e8},   # 再+1亿
    ])
    prov = _InstProvider({"20260617": d1, "20260618": d2})
    m = FT._inst_net_map(prov, ["20260617", "20260618"])
    # 新结构：{ts: (净买亿, 净买天数)}
    assert round(m["600000.SH"][0], 2) == 4.0       # 3 + 1，游资不计
    assert m["600000.SH"][1] == 2                    # 两日均净买 → 2 个吸筹日
    assert round(m["000001.SZ"][0], 2) == -2.0
    assert m["000001.SZ"][1] == 0                    # 净卖 → 0 个吸筹日


def test_inst_net_map_empty_safe() -> None:
    """缺数据/空表优雅跳过，不抛异常。"""
    prov = _InstProvider({"20260618": pd.DataFrame()})
    assert FT._inst_net_map(prov, ["20260618", "20260617"]) == {}


# ---------------------------------------------------------------------------
# 2b. 三源一致性打分 _consistency（纯函数·四档）
# ---------------------------------------------------------------------------

def test_consistency_resonance_needs_real_money() -> None:
    """机构真买 + 主力流入 + 北向正 → 高分『三源共振·偏多』。"""
    s, label = FT._consistency(8.0, 5.0, on_lhb=True, north_market_yi=1.0, inst_buy_days=1)
    assert s >= FT._TH_RESONANCE and label == "三源共振·偏多"


def test_consistency_no_real_money_capped_below_resonance() -> None:
    """仅主力估算流入(无机构足迹) → 偏多但封在共振档以下(无真钱印证)。"""
    s, label = FT._consistency(3.0, 0.0, on_lhb=False, north_market_yi=0.0)
    assert FT._TH_BULLISH <= s < FT._TH_RESONANCE and label == "资金偏多"


def test_consistency_divergence_is_bearish() -> None:
    """主力流入但机构净卖 → 背离·偏空。"""
    s, label = FT._consistency(4.0, -3.0, on_lhb=True, north_market_yi=0.0)
    assert s <= FT._TH_BEARISH and label == "资金偏空·背离"


def test_consistency_persistence_boosts_score() -> None:
    """同等净买下，机构净买天数越多分越高（持续吸筹置信更高）。"""
    s1, _ = FT._consistency(0.0, 2.0, on_lhb=True, north_market_yi=0.0, inst_buy_days=1)
    s3, _ = FT._consistency(0.0, 2.0, on_lhb=True, north_market_yi=0.0, inst_buy_days=3)
    assert s3 > s1


def test_consistency_neutral_when_no_signal() -> None:
    """三路均无方向 → 基线中性。"""
    s, label = FT._consistency(0.0, 0.0, on_lhb=False, north_market_yi=0.0)
    assert s == FT._SCORE_BASE and label == "资金中性"


def test_consistency_clamped_0_100() -> None:
    """极端输入分数被夹在 0~100。"""
    hi, _ = FT._consistency(99.0, 99.0, on_lhb=True, north_market_yi=9.0, inst_buy_days=9)
    lo, _ = FT._consistency(-99.0, -99.0, on_lhb=True, north_market_yi=-9.0)
    assert 0 <= lo <= 100 and 0 <= hi <= 100


# ---------------------------------------------------------------------------
# 3. 端到端 build_fund_triangle（注入式 FakeProvider）
# ---------------------------------------------------------------------------

class _FakeProvider:
    """打桩 build_fund_triangle 依赖的三接口：日历 / 龙虎榜机构 / 北向。"""

    def __init__(self, inst_df: pd.DataFrame, north_money: str):
        self._inst_df = inst_df
        self._north = north_money

    def get_trade_cal(self, start: str, end: str) -> pd.DataFrame:
        days = ["20260616", "20260617", "20260618"]
        return pd.DataFrame({"cal_date": days, "is_open": [1, 1, 1]})

    def get_lhb_inst(self, trade_date: str) -> pd.DataFrame:
        # 只在最后一日有机构上榜，其余日为空
        return self._inst_df if trade_date == "20260618" else pd.DataFrame()

    def get_north_flow(self, trade_date: str) -> pd.DataFrame:
        return pd.DataFrame([{"trade_date": trade_date, "north_money": self._north}])


def test_build_triangle_confirm_and_no_trace() -> None:
    inst_df = _lhb_inst_df([
        {"ts_code": "603986.SH", "exalter": "机构专用", "net_buy": 5e8},   # 5亿真买
    ])
    prov = _FakeProvider(inst_df, north_money="429343.56")   # 万元 → 42.93亿
    main_flow = {"603986.SH": 8.0, "300308.SZ": 3.0}         # 两只主力均流入
    res = FT.build_fund_triangle(prov, "20260618", main_flow, lookback=3)

    # 603986 有机构真买 → 真钱印证 + 一致性进共振档 + 1 个吸筹日
    a = res["603986.SH"]
    assert a.label == FT._L_CONFIRM and a.on_lhb and round(a.inst_net_yi, 2) == 5.0
    assert a.consistency >= FT._TH_RESONANCE and a.consistency_label == "三源共振·偏多"
    assert a.inst_buy_days == 1
    # 300308 主力流入但无机构足迹 → 机构无足迹 + 偏多但未达共振(无真钱印证)
    b = res["300308.SZ"]
    assert b.label == FT._L_NO_TRACE and not b.on_lhb
    assert FT._TH_BULLISH <= b.consistency < FT._TH_RESONANCE and b.inst_buy_days == 0
    # 大盘北向环境背景两只共享
    assert a.north_market_yi == b.north_market_yi == round(429343.56 / 1e4, 2)


def test_build_triangle_diverge() -> None:
    """主力流入但机构在龙虎榜净卖 → 背离警示。"""
    inst_df = _lhb_inst_df([
        {"ts_code": "002156.SZ", "exalter": "机构专用", "net_buy": -3e8},  # 机构净卖3亿
    ])
    prov = _FakeProvider(inst_df, north_money="0")
    res = FT.build_fund_triangle(prov, "20260618", {"002156.SZ": 4.0}, lookback=3)
    t = res["002156.SZ"]
    assert t.label == FT._L_DIVERGE and t.level == "warn" and t.inst_net_yi == -3.0


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_fund_triangle 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
