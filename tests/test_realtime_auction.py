"""集合竞价：时段判定 + 自选/持仓竞价异动（纯函数·不连网）。"""

from __future__ import annotations

import datetime

from app.strategy.realtime_fund import (auction_alerts, auction_movers, auction_sector_strength,
                                        auction_sentiment, entrust_ratio)
from app.strategy.realtime_hub import market_session


def _ts(y: int, m: int, d: int, hh: int, mm: int) -> float:
    return datetime.datetime(y, m, d, hh, mm).timestamp()


def test_market_session_windows() -> None:
    # 2026-06-29 周一
    assert market_session(_ts(2026, 6, 29, 9, 18)) == "auction"        # 9:15-9:20 可撤单
    assert market_session(_ts(2026, 6, 29, 9, 22)) == "auction_lock"   # 9:20-9:25 不可撤单(关键)
    assert market_session(_ts(2026, 6, 29, 9, 26)) == "pre_open"       # 9:25-9:30 过渡
    assert market_session(_ts(2026, 6, 29, 9, 35)) == "continuous"
    assert market_session(_ts(2026, 6, 29, 13, 30)) == "continuous"
    assert market_session(_ts(2026, 6, 29, 8, 50)) == "closed"         # 9:15 前
    assert market_session(_ts(2026, 6, 29, 11, 45)) == "closed"        # 午间
    assert market_session(_ts(2026, 6, 29, 15, 30)) == "closed"        # 收盘后
    # 2026-06-28 周日 → 永远 closed
    assert market_session(_ts(2026, 6, 28, 9, 18)) == "closed"


def test_auction_gap_up_down() -> None:
    rows = [{"ts_code": "600522.SH", "price": 7.7, "pct_chg": 8.5, "name": "中天科技"},
            {"ts_code": "002179.SZ", "price": 40.0, "pct_chg": -9.0, "name": "中航光电"}]
    watch = {"600522.SH": {"name": "中天科技", "is_holding": False, "stop_loss": None},
             "002179.SZ": {"name": "中航光电", "is_holding": True, "stop_loss": None}}
    out = auction_alerts(rows, watch)
    keys = {k for k, *_ in out}
    assert "auc_up_600522.SH" in keys and "auc_down_002179.SZ" in keys
    titles = {k: t for k, t, _, _ in out}
    assert "持仓" in titles["auc_down_002179.SZ"] and "自选" in titles["auc_up_600522.SH"]


def test_auction_stop_break_takes_priority() -> None:
    rows = [{"ts_code": "X", "price": 9.0, "pct_chg": -3.0, "name": "测试"}]
    watch = {"X": {"name": "测试", "is_holding": True, "stop_loss": 9.5}}   # 现价跌破止损
    out = auction_alerts(rows, watch)
    assert len(out) == 1 and out[0][0] == "auc_stop_X"


def test_auction_small_gap_no_alert() -> None:
    rows = [{"ts_code": "X", "price": 10.0, "pct_chg": 3.0, "name": "测试"}]
    watch = {"X": {"name": "测试", "is_holding": False, "stop_loss": None}}
    assert auction_alerts(rows, watch) == []          # +3% 未达 7% 阈值


def test_auction_missing_quote_skipped() -> None:
    watch = {"X": {"name": "测试", "is_holding": False, "stop_loss": None}}
    assert auction_alerts([], watch) == []            # 快照里没有该票 → 跳过


def _mkt_rows() -> list[dict]:
    # 半导体集体高开、煤炭普跌
    return [
        {"ts_code": "A.SZ", "name": "芯片甲", "pct_chg": 9.8, "price": 11.0},
        {"ts_code": "B.SZ", "name": "芯片乙", "pct_chg": 6.0, "price": 22.0},
        {"ts_code": "C.SZ", "name": "芯片丙", "pct_chg": 4.2, "price": 33.0},
        {"ts_code": "D.SH", "name": "煤炭甲", "pct_chg": -3.0, "price": 8.0},
        {"ts_code": "E.SH", "name": "煤炭乙", "pct_chg": -9.7, "price": 5.0},
        {"ts_code": "F.SH", "name": "煤炭丙", "pct_chg": -1.0, "price": 6.0},
    ]


_IMAP = {"A.SZ": "半导体", "B.SZ": "半导体", "C.SZ": "半导体",
         "D.SH": "煤炭", "E.SH": "煤炭", "F.SH": "煤炭"}


def test_auction_sector_strength_ranks_by_gap() -> None:
    out = auction_sector_strength(_mkt_rows(), _IMAP, min_n=3)
    assert out[0]["industry"] == "半导体" and out[0]["avg_gap"] > 0      # 半导体均高开居首
    assert out[0]["leader"] == "芯片甲" and out[0]["n"] == 3
    assert out[-1]["industry"] == "煤炭" and out[-1]["avg_gap"] < 0


def test_auction_sector_min_n_filter() -> None:
    rows = [{"ts_code": "A.SZ", "name": "甲", "pct_chg": 5.0}]
    assert auction_sector_strength(rows, {"A.SZ": "半导体"}, min_n=3) == []   # 不足3只不计


def test_auction_sentiment_counts_and_state() -> None:
    s = auction_sentiment(_mkt_rows())
    assert s["up"] == 3 and s["down"] == 3
    assert s["limit_up"] == 1 and s["limit_down"] == 1                    # 9.8↑ / -9.7↓
    assert s["state"] == "高低分歧"
    assert auction_sentiment([]) == {}


def test_auction_movers_high_low() -> None:
    m = auction_movers(_mkt_rows(), _IMAP, top=2)
    assert [x["name"] for x in m["high"]] == ["芯片甲", "芯片乙"]
    assert m["high"][0]["industry"] == "半导体"
    assert m["low"][0]["name"] == "煤炭乙"                                  # -9.7 最低


def test_entrust_ratio() -> None:
    assert entrust_ratio([100, 50], [30, 20]) == 50.0                     # 委买150 vs 委卖50 → +50%承接
    assert entrust_ratio([10], [90]) == -80.0                            # 抛压
    assert entrust_ratio([], []) == 0.0 and entrust_ratio(None, None) == 0.0


def test_auction_movers_orderbook_and_seal() -> None:
    rows = [{"ts_code": "A.SZ", "name": "一字板", "pct_chg": 10.0, "price": 11.0,
             "limit_up": 11.0, "amount": 3.2e8, "vol_ratio": 8.5,
             "bid_vol": [900, 100], "ask_vol": [0, 0]}]
    h = auction_movers(rows, {"A.SZ": "半导体"}, top=5)["high"][0]
    assert h["seal_up"] is True                                          # 价==涨停价 → 一字
    assert h["amount_yi"] == 3.2 and h["vol_ratio"] == 8.5
    assert h["entrust"] == 100.0                                         # 全是委买·满承接


def test_auction_sector_aggregates_amount_and_entrust() -> None:
    rows = [{"ts_code": "A.SZ", "name": "甲", "pct_chg": 5.0, "amount": 1e8,
             "bid_vol": [80], "ask_vol": [20]},
            {"ts_code": "B.SZ", "name": "乙", "pct_chg": 3.0, "amount": 1e8,
             "bid_vol": [60], "ask_vol": [40]},
            {"ts_code": "C.SZ", "name": "丙", "pct_chg": 4.0, "amount": 1e8,
             "bid_vol": [60], "ask_vol": [40]}]
    s = auction_sector_strength(rows, {"A.SZ": "半导体", "B.SZ": "半导体", "C.SZ": "半导体"}, min_n=3)[0]
    assert s["amount_yi"] == 3.0                                         # 三只各1亿
    assert s["entrust"] == 33.3                                          # 委买200/委卖100 → (200-100)/300=+33.3%


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_realtime_auction 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
