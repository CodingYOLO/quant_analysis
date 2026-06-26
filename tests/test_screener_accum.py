"""慢牛吸筹因子单测：吸筹评分(纯函数) + 多日因子矩阵计算（零网络·合成数据）。

覆盖 2026-06 据策略复盘新增的"量价配合""振幅收敛"，以及大单估算降为弱信号的配权。
"""

from __future__ import annotations

import math

import pandas as pd

import app.strategy.screener as SC

# 理想吸筹一组参数（各项均落甜区 → 满分）
_IDEAL = dict(vol_ratio=1.6, ma20_slope=6.0, ret20=12.0, amp20=3.0,
              big_up_days=0, main_net_3d=1.0, up_down_vol=1.5, amp_contract=0.8)


# ---------------------------------------------------------------------------
# 1. 吸筹评分 _accumulation_score（甜区给分·NaN兜底·夹值）
# ---------------------------------------------------------------------------

def test_score_ideal_full_marks() -> None:
    assert SC._accumulation_score(**_IDEAL) == 100.0


def test_score_explosive_volume_loses_vol_points() -> None:
    """爆量(量比5)→ 量能项(18)不得分。"""
    p = {**_IDEAL, "vol_ratio": 5.0}
    assert SC._accumulation_score(**p) == 100.0 - SC._ACC_W["vol"]


def test_score_volprice_rewards_healthy() -> None:
    """量价配合(涨放量/跌缩量)越健康分越高。"""
    healthy = SC._accumulation_score(**{**_IDEAL, "up_down_vol": 1.5})
    weak = SC._accumulation_score(**{**_IDEAL, "up_down_vol": 0.8})
    assert healthy > weak and healthy - weak == SC._ACC_W["volprice"]


def test_score_amp_contraction_rewards_locking() -> None:
    """振幅收敛(近<前)给分，振幅扩张不给分。"""
    contracting = SC._accumulation_score(**{**_IDEAL, "amp_contract": 0.8})
    expanding = SC._accumulation_score(**{**_IDEAL, "amp_contract": 1.3})
    assert contracting - expanding == SC._ACC_W["contract"]


def test_score_not_hidden_when_big_up_days() -> None:
    """近20日大涨3天 → 隐蔽项(12)被吃光。"""
    exposed = SC._accumulation_score(**{**_IDEAL, "big_up_days": 3})
    assert exposed == 100.0 - SC._ACC_W["hidden"]


def test_score_fund_is_weak_signal() -> None:
    """大单估算仅 8 分（弱信号）：流入vs流出只差 8。"""
    s_in = SC._accumulation_score(**{**_IDEAL, "main_net_3d": 1.0})
    s_out = SC._accumulation_score(**{**_IDEAL, "main_net_3d": -1.0})
    assert s_in - s_out == SC._ACC_W["fund"] == 8


def test_score_nan_safe_zero() -> None:
    nan = float("nan")
    assert SC._accumulation_score(nan, nan, nan, nan, nan, nan,
                                  up_down_vol=nan, amp_contract=nan) == 0.0


def test_inflow_counts() -> None:
    """反复净流入计数：净流入天数 + 连续(从最新往回·遇流出/缺数据断)。"""
    # 升序：旧→新。末尾两天为正 → 连续2；总正天数=4
    assert SC._inflow_counts([1, -1, 2, -1, 3, 4]) == (4, 2)
    assert SC._inflow_counts([1, 2, 3]) == (3, 3)            # 全正 → 连续到底
    assert SC._inflow_counts([1, 2, -1]) == (2, 0)           # 最新为负 → 连续0
    assert SC._inflow_counts([1, 2, None]) == (2, 0)         # 最新缺数据 → 连续中断
    assert SC._inflow_counts([None, None]) == (0, 0)


def test_sector_strength_flag() -> None:
    """板块走强：行业RPS中位≥55且≥3只→True；弱行业/不足3只→False。"""
    df = pd.DataFrame({
        "industry": ["半导体"] * 3 + ["钢铁"] * 3 + ["小行业"] * 2,
        "rps120": [80, 70, 60,   30, 20, 10,   90, 95],   # 半导体中位70强 / 钢铁中位20弱 / 小行业仅2只
    })
    out = SC._add_sector_strength_flag(df)
    by_ind = dict(zip(out["industry"], out["sector_strong"]))
    assert by_ind["半导体"] is True or out[out.industry == "半导体"]["sector_strong"].all()
    assert not out[out.industry == "钢铁"]["sector_strong"].any()      # 弱板块
    assert not out[out.industry == "小行业"]["sector_strong"].any()    # 不足3只


def test_score_clamped_to_100() -> None:
    s = SC._accumulation_score(1.6, 5.0, 12.0, 1.0, 0, 99.0, up_down_vol=9.0, amp_contract=0.1)
    assert 0.0 <= s <= 100.0


# ---------------------------------------------------------------------------
# 2. 多日因子矩阵 _accum_factor_columns（合成 30 日矩阵·向量化）
# ---------------------------------------------------------------------------

def _slow_bull_matrix():
    """构造"温和放量·缓慢走高·涨放量跌缩量"的 30 日票 A（含小回调以便算量价配合）。"""
    pattern = [1, 1, -1, 1, 1, 1, -1, 1, 1, 1] * 3        # 上涨为主、周期性小回调
    price, closes, vols = 10.0, [], []
    for d in pattern:
        price += 0.06 if d > 0 else -0.03
        closes.append(round(price, 3))
        vols.append(140 if d > 0 else 80)                 # 上涨放量、回调缩量
    close = pd.DataFrame({"A": closes})
    return close, close + 0.08, close - 0.08, pd.DataFrame({"A": vols})


def test_accum_columns_slow_bull() -> None:
    close, high, low, vol = _slow_bull_matrix()
    cols = SC._accum_factor_columns(close, high, low, vol)
    assert cols["ret20"]["A"] > 0                 # 净走高
    assert "ret5" in cols and cols["ret5"]["A"] > 0   # 近5日涨幅(超跌低吸用·此处为涨)
    assert cols["ma20_slope"]["A"] > 0            # MA20向上
    assert cols["big_up_days_20"]["A"] == 0       # 无大涨 → 隐蔽
    assert cols["up_down_vol"]["A"] > 1.3         # 涨放量/跌缩量 ≈ 140/80
    assert cols["amp20"]["A"] < 5                 # 低振幅
    assert "amp_contract" in cols                 # 收敛比已产出


def test_accum_columns_counts_big_up_days() -> None:
    """含一个 +10% 跳涨日 → big_up_days_20 计为 1。"""
    seq = [10.0] * 20 + [11.0] + [11.05 + 0.01 * i for i in range(9)]
    close = pd.DataFrame({"B": seq})
    cols = SC._accum_factor_columns(close, close + 0.1, close - 0.1, pd.DataFrame({"B": [100] * 30}))
    assert cols["big_up_days_20"]["B"] == 1


def test_limit_stats_counts_and_consec() -> None:
    """主板(9.8%)：三个涨停日、其中两连板。"""
    close = pd.Series([10.0, 11.0, 12.1, 12.1, 13.31])   # 涨幅%≈ [_,10,10,0,10]
    ups, mx = SC._limit_stats(close, 9.8)
    assert ups == 3 and mx == 2


def test_limit_stats_board_threshold() -> None:
    """创业板涨停≈20%，+10% 不算涨停。"""
    ups, mx = SC._limit_stats(pd.Series([10.0, 11.0, 12.1]), 19.8)
    assert ups == 0 and mx == 0


def test_youzi_relay_map_counts_recurring() -> None:
    """跨多日聚合游资席位：A两日游资买(接力)、B仅1日(d1是机构不算游资)。"""
    class _P:
        def get_trade_cal(self, s, e):
            return pd.DataFrame({"cal_date": ["20260601", "20260602", "20260603"], "is_open": [1, 1, 1]})

        def get_lhb_inst(self, d):
            data = {
                "20260601": [("A.SZ", "上海溧阳路证券营业部", 1e8), ("B.SZ", "机构专用", 1e8)],
                "20260602": [("A.SZ", "某券商溧阳路营业部", 0.5e8)],
                "20260603": [("B.SZ", "佛山某证券营业部", 0.3e8)],
            }
            return pd.DataFrame([{"ts_code": t, "exalter": x, "net_buy": n, "buy": n, "sell": 0}
                                 for t, x, n in data.get(d, [])])

    m = SC._youzi_relay_map(_P(), "20260603", lookback=20)
    assert m["A.SZ"][0] == 2 and round(m["A.SZ"][1], 2) == 1.5    # 两日接力·净买1.5亿
    assert m["B.SZ"][0] == 1                                       # 仅游资买那1日(机构日不计)


def test_leader_flags_top2_per_industry() -> None:
    """行业内按龙头分前2标 is_leader；小弱票不标。"""
    df = pd.DataFrame({
        "ts_code": ["a", "b", "c", "d"],
        "industry": ["甲", "甲", "甲", "乙"],
        "rps120": [90, 80, 20, 70],
        "circ_mv_100m": [500, 300, 50, 200],
        "amount_100m": [10, 8, 1, 5],
    })
    out = SC._add_leader_flags(df, top_n=2)
    flags = dict(zip(out["ts_code"], out["is_leader"]))
    assert flags["a"] and flags["b"] and not flags["c"]   # 甲行业前2=a,b
    assert flags["d"]                                       # 乙行业唯一→龙头


def test_forecast_periods_picks_h1_in_june() -> None:
    """6月底：中报(0630·允许未来15天) + 一季报(0331)，中报在前。"""
    import datetime
    assert SC._forecast_periods(datetime.date(2026, 6, 23)) == ["20260630", "20260331"]


def test_add_earnings_prefers_h1_and_flags_good() -> None:
    """每股取最新期(中报优先)；预喜标记；forecast_chg取中值。"""
    class _P:
        def get_forecast_by_period(self, period):
            data = {
                "20260630": [("A.SZ", "20260623", "预增", 100, 160)],     # A有中报预告·预增
                "20260331": [("A.SZ", "20260429", "略减", -10, -5),       # A一季报(应被中报覆盖)
                             ("B.SZ", "20260429", "首亏", -200, -150)],   # B只有一季报·预亏
            }
            return pd.DataFrame([{"ts_code": t, "ann_date": a, "type": ty,
                                  "p_change_min": lo, "p_change_max": hi}
                                 for t, a, ty, lo, hi in data.get(period, [])])

    df = pd.DataFrame({"ts_code": ["A.SZ", "B.SZ", "C.SZ"]})
    out = SC._add_earnings(df, _P())
    a = out[out["ts_code"] == "A.SZ"].iloc[0]
    assert a["forecast_type"] == "预增" and a["forecast_chg"] == 130.0   # 中报覆盖一季报·(100+160)/2
    assert a["earn_good"] and a["is_h1_forecast"]
    b = out[out["ts_code"] == "B.SZ"].iloc[0]
    assert b["forecast_type"] == "首亏" and not b["earn_good"] and not b["is_h1_forecast"]
    c = out[out["ts_code"] == "C.SZ"].iloc[0]
    assert not c["earn_good"] and not c["is_h1_forecast"]                 # 无预告


def test_latest_fina_period() -> None:
    import datetime
    assert SC._latest_fina_period(datetime.date(2026, 6, 22)) == "20260331"
    assert SC._latest_fina_period(datetime.date(2026, 9, 1)) == "20260630"
    assert SC._latest_fina_period(datetime.date(2026, 2, 1)) == "20250930"


def test_accum_columns_short_history_skips() -> None:
    """历史不足(<21日)→ 不产出涨幅/隐蔽列，不报错。"""
    close = pd.DataFrame({"C": [10.0 + i for i in range(10)]})
    cols = SC._accum_factor_columns(close, close + 0.1, close - 0.1,
                                    pd.DataFrame({"C": [100] * 10}))
    assert "ret20" not in cols and "big_up_days_20" not in cols


# ---------------------------------------------------------------------------
# runner（无 pytest 依赖）
# ---------------------------------------------------------------------------

def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_screener_accum 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
