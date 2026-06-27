"""实时资金/异动分析：纯函数单测（零网络）。"""

from __future__ import annotations

import pandas as pd

from app.strategy.realtime_fund import (active_net_yi, detect_limit_breaks,
                                        detect_theme_fermentation, fund_ranking,
                                        fund_surge_events, holding_health, outer_ratio,
                                        sector_board, sector_flow_events, velocity_events)


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
