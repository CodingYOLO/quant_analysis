"""实时资金/异动分析：纯函数单测（零网络）。"""

from __future__ import annotations

import pandas as pd

from app.strategy.realtime_fund import (active_net_yi, detect_flash_crashes,
                                        detect_limit_breaks, detect_theme_fermentation,
                                        fund_ranking, fund_surge_events, holding_health,
                                        outer_ratio, sector_board, sector_flow_events,
                                        tail_baseline_of, tail_movers, tail_sector_flow,
                                        velocity_events)


def _df() -> pd.DataFrame:
    return pd.DataFrame([
        {"ts_code": "688256.SH", "name": "寒武纪", "price": 20.0, "pct_chg": 5.0,
         "vol_ratio": 3.0, "inner": 100000.0, "outer": 500000.0},     # net=8.0亿·抢筹
        {"ts_code": "300308.SZ", "name": "中际旭创", "price": 10.0, "pct_chg": 4.0,
         "vol_ratio": 2.5, "inner": 200000.0, "outer": 300000.0},     # net=1.0亿·外盘0.60不达抢筹
        {"ts_code": "000002.SZ", "name": "某地产", "price": 5.0, "pct_chg": -3.0,
         "vol_ratio": 1.0, "inner": 300000.0, "outer": 100000.0},     # net=-1.0亿·主动卖
    ])


def test_active_net_yi_and_ratio() -> None:
    assert active_net_yi(445825, 526412, 10.23) == 0.8244       # 文档浦发样本
    assert active_net_yi(300000, 100000, 5.0) == -1.0           # 主动卖出为负
    assert outer_ratio(100000, 500000) == round(500000 / 600000, 4)
    assert outer_ratio(0, 0) == 0.5                             # 无成交中性


def test_fund_ranking_order() -> None:
    rk = fund_ranking(_df(), top=10)
    assert [r["ts_code"] for r in rk] == ["688256.SH", "300308.SZ", "000002.SZ"]
    assert rk[0]["net_yi"] == 8.0 and rk[0]["outer_ratio"] == round(500000 / 600000, 3)
    assert rk[2]["net_yi"] == -1.0                              # 主动卖在最后


def _sec_df() -> pd.DataFrame:
    """每板块≥3只（满足成分数门槛）：CPO 资金涌入、房地产 资金撤离。"""
    return pd.DataFrame([
        {"ts_code": "688256.SH", "name": "寒武纪", "price": 20.0, "pct_chg": 5.0, "vol_ratio": 3, "inner": 100000.0, "outer": 500000.0},
        {"ts_code": "300308.SZ", "name": "中际旭创", "price": 10.0, "pct_chg": 4.0, "vol_ratio": 2, "inner": 200000.0, "outer": 300000.0},
        {"ts_code": "300502.SZ", "name": "新易盛", "price": 100.0, "pct_chg": 6.0, "vol_ratio": 3, "inner": 50000.0, "outer": 150000.0},   # net=10·龙头
        {"ts_code": "000002.SZ", "name": "万科A", "price": 5.0, "pct_chg": -3.0, "vol_ratio": 1, "inner": 300000.0, "outer": 100000.0},
        {"ts_code": "600340.SH", "name": "华夏幸福", "price": 10.0, "pct_chg": -2.0, "vol_ratio": 1, "inner": 200000.0, "outer": 100000.0},
        {"ts_code": "001979.SZ", "name": "招商蛇口", "price": 8.0, "pct_chg": -4.0, "vol_ratio": 1, "inner": 400000.0, "outer": 200000.0},
    ])


_SEC_IMAP = {"688256.SH": "CPO", "300308.SZ": "CPO", "300502.SZ": "CPO",
             "000002.SZ": "房地产", "600340.SH": "房地产", "001979.SZ": "房地产"}


def test_sector_board_with_leader() -> None:
    board = sector_board(_sec_df(), _SEC_IMAP)
    assert board[0]["industry"] == "CPO" and board[0]["net_yi"] == 19.0 and board[0]["n"] == 3
    assert board[0]["leader"] == "新易盛" and board[0]["leader_pct"] == 6.0   # 龙头=板块内吸金最多
    assert board[-1]["industry"] == "房地产" and board[-1]["net_yi"] == -3.6
    assert sector_board(_df(), _SEC_IMAP) == []                              # 每板块<3只 → 不计


def test_sector_flow_events_in_and_out() -> None:
    ev = sector_flow_events(sector_board(_sec_df(), _SEC_IMAP))
    kinds = {e["industry"]: e["kind"] for e in ev}
    assert kinds["CPO"] == "in" and kinds["房地产"] == "out"                 # 涌入机会 / 撤离风险
    assert next(e for e in ev if e["kind"] == "in")["leader"] == "新易盛"


def test_fund_surge_only_qualified() -> None:
    hits = fund_surge_events(_df())
    assert [h["ts_code"] for h in hits] == ["688256.SH"]        # 仅寒武纪四条件全达标
    assert hits[0]["net_yi"] == 8.0


def test_velocity_events() -> None:
    now = {"X": 11.0, "Y": 10.1, "Z": 10.0}
    past = {"X": 10.0, "Y": 10.0, "Z": 10.0}
    ev = velocity_events(now, past, min_move=2.0)
    assert [e["ts_code"] for e in ev] == ["X"] and ev[0]["move"] == 10.0


def test_holding_health() -> None:
    assert holding_health({"pct_chg": -6, "inner": 1, "outer": 1, "price": 9}, None)[0] == "留意"
    assert holding_health({"pct_chg": 2, "inner": 100, "outer": 200, "price": 9}, None)[0] == "健康"
    assert holding_health({"pct_chg": 1, "inner": 200, "outer": 100, "price": 9}, None)[0] == "留意"
    assert holding_health({"pct_chg": 1, "inner": 100, "outer": 100, "price": 5}, 6.0)[0] == "风险"


def _sealed_row(code, price, lu, bid1, amount=2e8, pct=10.0):
    return {"ts_code": code, "name": code[:4], "price": price, "limit_up": lu, "pct_chg": pct,
            "amount": amount, "bid_vol": [bid1, 0, 0, 0, 0]}


def test_limit_break_lifecycle() -> None:
    """封板 → 封单萎缩(开板预警) → 脱板(炸板),三态转换。"""
    sealed: dict = {}
    ev1, sealed = detect_limit_breaks([_sealed_row("A.SH", 11.0, 11.0, 10000)], sealed)
    assert ev1 == [] and "A.SH" in sealed                         # 首次封板·无事件
    ev2, sealed = detect_limit_breaks([_sealed_row("A.SH", 11.0, 11.0, 3000)], sealed)
    assert any(k.startswith("limitweak_") for k, *_ in ev2)       # 封单3000<峰值40% → 开板预警
    ev3, sealed = detect_limit_breaks([_sealed_row("A.SH", 10.5, 11.0, 0, pct=5.0)], sealed)
    assert any(k == "limitbreak_A.SH" for k, *_ in ev3) and "A.SH" not in sealed   # 脱板=炸板


def test_limit_break_filters_small_amount() -> None:
    ev, sealed = detect_limit_breaks([_sealed_row("B.SH", 11.0, 11.0, 9999, amount=2e7)], {})
    assert ev == [] and sealed == {}                              # 成交额<1亿 不跟踪


def test_theme_fermentation() -> None:
    cmap = {"AI算力": ["1.SH", "2.SH", "3.SH", "4.SH"], "银行": ["5.SH", "6.SH"]}
    rows = [{"ts_code": f"{i}.SH", "name": f"票{i}", "pct_chg": p, "amount": 1e8}
            for i, p in [("1", 8.0), ("2", 6.0), ("3", 5.5), ("4", 2.0), ("5", 7.0), ("6", 6.0)]]
    themes = detect_theme_fermentation(rows, cmap, min_hot=3, min_pct=5.0)
    assert [t["theme"] for t in themes] == ["AI算力"]             # AI 3只达标;银行仅2只不算
    assert themes[0]["n_hot"] == 3 and themes[0]["leaders"][0]["name"] == "票1"


def _tail_row(code, price, inner, outer, amount=2e8, pct=0.0):
    return {"ts_code": code, "name": code[:4], "price": price, "inner": inner,
            "outer": outer, "amount": amount, "pct_chg": pct}


def test_tail_baseline_and_movers() -> None:
    """以14:30为基准：尾盘+主动买=拉升、尾盘-主动卖=跳水，小幅/小额剔除。"""
    base = tail_baseline_of([_tail_row("A.SH", 10, 100000, 100000),     # net=0
                             _tail_row("B.SH", 20, 100000, 100000),
                             _tail_row("C.SH", 5, 100000, 100000)])
    now = [_tail_row("A.SH", 10.3, 100000, 400000),                     # +3%·主动买 → 拉升
           _tail_row("B.SH", 19.4, 400000, 100000),                     # -3%·主动卖 → 跳水
           _tail_row("C.SH", 5.05, 100000, 400000),                     # +1% 不足2% → 剔除
           _tail_row("D.SH", 99, 1, 9e9)]                               # 无基准 → 剔除
    mv = {m["ts_code"]: m["kind"] for m in tail_movers(now, base)}
    assert mv == {"A.SH": "up", "B.SH": "down"}


def test_tail_movers_filters_small_amount() -> None:
    base = tail_baseline_of([_tail_row("A.SH", 10, 100000, 100000)])
    now = [_tail_row("A.SH", 10.5, 100000, 500000, amount=2e7)]         # 涨5%但成交额<1亿
    assert tail_movers(now, base) == []


def test_tail_sector_flow() -> None:
    base = tail_baseline_of([_tail_row(c, 10, 100000, 100000) for c in ("A.SH", "B.SH", "C.SH")])
    now = [_tail_row("A.SH", 10, 100000, 300000), _tail_row("B.SH", 10, 100000, 300000),
           _tail_row("C.SH", 10, 100000, 200000)]                       # 三只均主动买流入
    imap = {"A.SH": "CPO", "B.SH": "CPO", "C.SH": "CPO"}
    sf = tail_sector_flow(now, base, imap)
    assert sf[0]["industry"] == "CPO" and sf[0]["n"] == 3 and sf[0]["net_tail"] > 0


def test_flash_crashes_tiers() -> None:
    """闪崩=极速跌+放量+主动卖;急跌=只够速度;小幅/小额剔除。"""
    past = {"A.SH": 10.0, "B.SH": 10.0, "C.SH": 10.0, "D.SH": 10.0}
    rows = [
        {"ts_code": "A.SH", "name": "闪崩", "price": 9.3, "amount": 2e8, "vol_ratio": 2.0,
         "inner": 300000, "outer": 100000, "pct_chg": -8},      # -7%·放量·内盘主导 → crash
        {"ts_code": "B.SH", "name": "急跌", "price": 9.5, "amount": 2e8, "vol_ratio": 1.0,
         "inner": 100000, "outer": 100000, "pct_chg": -5},      # -5%·不放量 → warn
        {"ts_code": "C.SH", "name": "小跌", "price": 9.8, "amount": 2e8, "vol_ratio": 2.0,
         "inner": 300000, "outer": 100000, "pct_chg": -2},      # -2% 未达预警 → 无
        {"ts_code": "D.SH", "name": "小额", "price": 9.0, "amount": 2e7, "vol_ratio": 3.0,
         "inner": 300000, "outer": 100000, "pct_chg": -10},     # -10%但成交额<1亿 → 剔除
    ]
    res = {f["ts_code"]: f["tier"] for f in detect_flash_crashes(rows, past)}
    assert res == {"A.SH": "crash", "B.SH": "warn"}


def test_tech_tag() -> None:
    from app.strategy.realtime_fund import tech_tag
    assert tech_tag(None) == "" and tech_tag({}) == ""
    strong = tech_tag({"ma_bull_full": True, "pat_breakout_high_20": True,
                       "rps120": 92, "vol5_vol20": 1.8})
    assert "多头排列" in strong and "破20日高" in strong and "RPS92" in strong and "放量" in strong
    weak = tech_tag({"ma_bull_full": False, "above_ma20": False, "above_ma60": False,
                     "rps120": 35, "vol5_vol20": 0.6})
    assert "MA60下方" in weak and "RPS35弱" in weak and "缩量" in weak


def test_tech_context_live_levels() -> None:
    from app.strategy.realtime_fund import tech_context
    t = {"ma_bull_full": True, "close": 10.0, "ma20": 9.5, "ma60": 8.0,
         "high20": 10.5, "low20": 8.5, "rps120": 95, "vol5_vol20": 1.8}
    # 对齐(昨收10≈因子收盘10)·现价11破20日高
    assert "破20日高" in tech_context(11.0, 10.0, t) and "多头排列" in tech_context(11.0, 10.0, t)
    # 现价9跌破MA20
    assert "下MA20" in tech_context(9.0, 10.0, t)
    # 不对齐(昨收7 vs 因子收盘10·疑似除权)→ 不报数值位
    assert "破20日高" not in tech_context(11.0, 7.0, t) and "下MA20" not in tech_context(9.0, 7.0, t)


def test_detect_breakouts() -> None:
    from app.strategy.realtime_fund import detect_breakouts
    lv = {"A.SH": {"close": 10.0, "ma20": 9.5, "ma60": 8.0, "high20": 10.5, "low20": 8.5}}
    rows = [{"ts_code": "A.SH", "name": "甲", "price": 10.6, "prev_close": 10.0, "amount": 2e8, "pct_chg": 6}]
    past = {"A.SH": 10.2}                                       # 5分钟前10.2 < high20 10.5 ≤ 现10.6
    ev = detect_breakouts(rows, past, lv)
    assert ev and ev[0]["dir"] == "up" and ev[0]["what"] == "突破20日新高"
    # 跌破MA20：5分钟前9.6 ≥ 9.5 > 现9.4
    rows2 = [{"ts_code": "A.SH", "name": "甲", "price": 9.4, "prev_close": 10.0, "amount": 2e8, "pct_chg": -6}]
    ev2 = detect_breakouts(rows2, {"A.SH": 9.6}, lv)
    assert ev2 and ev2[0]["dir"] == "down" and "MA20" in ev2[0]["what"]
    # 不对齐(除权)→ 不判
    rows3 = [{"ts_code": "A.SH", "name": "甲", "price": 10.6, "prev_close": 7.0, "amount": 2e8, "pct_chg": 6}]
    assert detect_breakouts(rows3, past, lv) == []


def test_empty_inputs_safe() -> None:
    empty = pd.DataFrame()
    assert fund_ranking(empty) == [] and sector_board(empty, {}) == []
    assert fund_surge_events(empty) == [] and velocity_events({}, {}) == []


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_realtime_fund 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
