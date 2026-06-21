"""龙虎榜席位分类 + 资金风格 lhb_seats 单测。

零网络。直接运行：python -m tests.test_lhb_seats
"""

from __future__ import annotations

import pandas as pd

import app.strategy.lhb_seats as S


# ---- 1. 席位分类 ----

def test_classify_seat() -> None:
    assert S.classify_seat("机构专用")["type"] == "inst"
    assert S.classify_seat("深股通专用")["type"] == "north"
    assert S.classify_seat("沪股通专用")["type"] == "north"
    f = S.classify_seat("高盛(中国)证券有限责任公司上海浦东新区世纪大道证券营业部")
    assert f["type"] == "foreign" and f["nickname"] == "高盛"
    h = S.classify_seat("东方财富证券股份有限公司拉萨东环路第二证券营业部")
    assert h["type"] == "hot" and h["nickname"] == "拉萨天团"
    assert S.classify_seat("某普通证券营业部")["type"] == "normal"


# ---- 2. 席位明细去重 + 金额 ----

def _df(rows):
    return pd.DataFrame(rows, columns=["ts_code", "exalter", "buy", "sell", "net_buy", "reason"])


def test_seat_rows_dedup_and_amount() -> None:
    df = _df([
        {"ts_code": "1", "exalter": "深股通专用", "buy": 7.36e8, "sell": 9.62e8, "net_buy": -2.26e8, "reason": "振幅"},
        {"ts_code": "1", "exalter": "深股通专用", "buy": 7.36e8, "sell": 9.62e8, "net_buy": -2.26e8, "reason": "振幅"},  # 买卖两侧重复
        {"ts_code": "1", "exalter": "上海证券徐汇区高安路证券营业部", "buy": 2.12e8, "sell": 0.2e8, "net_buy": 1.92e8, "reason": "振幅"},
    ])
    seats = S.seat_rows(df)
    assert len(seats) == 2                                  # 重复去掉
    assert seats[0]["type"] == "hot" and seats[0]["net_yi"] == 1.92   # 净额降序·游资居首
    assert seats[1]["type"] == "north" and seats[1]["net_yi"] == -2.26


# ---- 3. 资金风格推断 ----

def test_infer_style_north_sell_hot_buy() -> None:
    """北向在卖、游资在买 → 北向流出 + 游资主导（兴森科技真实场景）。"""
    seats = [
        {"type": "north", "net_yi": -2.26, "buy_yi": 7.36, "sell_yi": 9.62},
        {"type": "hot", "net_yi": 1.92, "buy_yi": 2.12, "sell_yi": 0.2},
    ]
    st = S.infer_style(seats)
    texts = [t["text"] for t in st["tags"]]
    assert "游资主导" in texts and "北向流出" in texts
    assert "游资净+1.9亿" in st["note"]


def test_infer_style_inst_huddle() -> None:
    seats = [{"type": "inst", "net_yi": 0.7, "buy_yi": 1, "sell_yi": 0.3},
             {"type": "inst", "net_yi": 0.5, "buy_yi": 0.6, "sell_yi": 0.1}]
    assert any(t["text"] == "机构抱团" for t in S.infer_style(seats)["tags"])


def test_infer_style_inst_divergence() -> None:
    seats = [{"type": "inst", "net_yi": 1.0, "buy_yi": 1, "sell_yi": 0},
             {"type": "inst", "net_yi": -0.8, "buy_yi": 0, "sell_yi": 0.8}]
    assert any(t["text"] == "机构分歧" for t in S.infer_style(seats)["tags"])


def test_infer_style_empty() -> None:
    assert any(t["text"] == "多空混杂" for t in S.infer_style([])["tags"])


# ---- 4. 次日参考解读 ----

def test_interpret_inst_huddle_bull() -> None:
    seats = [{"type": "inst", "net_yi": 0.7}, {"type": "inst", "net_yi": 0.5}]
    v = S.interpret_next_day(seats, "日涨幅偏离值达到7%")
    assert v["level"] == "bull" and "抱团" in v["scenario"]


def test_interpret_inst_sell_hot_buy_game() -> None:
    """机构撤、游资接盘 → 博弈/警示。"""
    seats = [{"type": "inst", "net_yi": -0.6}, {"type": "hot", "net_yi": 1.2}]
    v = S.interpret_next_day(seats, "日涨幅达到15%")
    assert v["level"] == "warn" and "接盘" in v["scenario"] and "轻仓" in v["action"]


def test_interpret_weak_down() -> None:
    seats = [{"type": "inst", "net_yi": -0.3}]
    v = S.interpret_next_day(seats, "日跌幅偏离值达到7%")
    assert v["level"] == "warn" and "离场" in v["scenario"]


def test_interpret_hot_board_game() -> None:
    seats = [{"type": "hot", "net_yi": 1.0, "nickname": "拉萨天团"}]
    v = S.interpret_next_day(seats, "日涨幅偏离值达到7%")
    assert v["level"] == "game" and "游资" in v["scenario"]


def test_interpret_diverge_watch() -> None:
    seats = [{"type": "inst", "net_yi": 0.0}]
    v = S.interpret_next_day(seats, "日振幅值达到15%")
    assert v["level"] == "watch"


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_lhb_seats 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
