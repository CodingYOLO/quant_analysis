"""沪深全推报文解析 + 快照：纯函数/内存单测（零网络）。

样本取自幕数据 L1 全推官方文档 https://www.mushuju.com/L1_qtapi.html ，
用于固化 36 字段映射，防止字段错位回归。
"""

from __future__ import annotations

from app.data.fullpush.parser import (parse_message, parse_record,
                                       to_mushuju_code, to_ts_code)
from app.data.fullpush.snapshot import MarketSnapshot

# 官方文档示例（浦发银行）：36 字段，$ 分隔
_SAMPLE = ("SH600000$浦发银行$1770274800$10.15$10.28$10.13$10.23$972237.0$992816064.0$"
           "10.24$10.25$10.26$10.27$10.28$4687.0$6629.0$5942.0$9658.0$12182.0$"
           "10.23$10.22$10.21$10.2$10.19$1387.0$4617.0$4320.0$4897.0$1891.0$"
           "0.29$10.13$11.14$9.12$0.89$445825.0$526412.0")


def test_code_conversion() -> None:
    assert to_ts_code("SH600000") == "600000.SH"
    assert to_ts_code("SZ000001") == "000001.SZ"
    assert to_ts_code("BJ430047") == "430047.BJ"
    assert to_ts_code("garbage") == "garbage"        # 无法识别原样返回
    assert to_mushuju_code("600000.SH") == "SH600000"
    assert to_mushuju_code("000001.SZ") == "SZ000001"
    assert to_mushuju_code("noformat") == "noformat"


def test_parse_basic_fields() -> None:
    q = parse_record(_SAMPLE)
    assert q is not None
    assert q["ts_code"] == "600000.SH" and q["name"] == "浦发银行"
    assert q["price"] == 10.23 and q["prev_close"] == 10.13
    assert q["open"] == 10.15 and q["high"] == 10.28 and q["low"] == 10.13
    assert q["amount"] == 992816064.0 and q["vol"] == 972237.0
    assert q["ts"] == 1770274800


def test_parse_pct_and_fund_fields() -> None:
    q = parse_record(_SAMPLE)
    assert q["pct_chg"] == 0.99                       # (10.23/10.13-1)*100 ≈ 0.99
    assert q["turnover_rate"] == 0.29 and q["vol_ratio"] == 0.89
    assert q["limit_up"] == 11.14 and q["limit_down"] == 9.12
    assert q["inner"] == 445825.0 and q["outer"] == 526412.0   # 外盘>内盘=主动买多


def test_parse_five_levels() -> None:
    q = parse_record(_SAMPLE)
    assert q["ask_px"] == [10.24, 10.25, 10.26, 10.27, 10.28]
    assert q["ask_vol"] == [4687.0, 6629.0, 5942.0, 9658.0, 12182.0]
    assert q["bid_px"] == [10.23, 10.22, 10.21, 10.2, 10.19]
    assert q["bid_vol"] == [1387.0, 4617.0, 4320.0, 4897.0, 1891.0]


def test_parse_incomplete_returns_none() -> None:
    assert parse_record("SH600000$浦发$10.23") is None     # 字段不足
    assert parse_record("") is None


def test_parse_zero_prev_close_safe() -> None:
    """昨收为 0（次新/停牌）不应除零，pct 归 0。"""
    fields = _SAMPLE.split("$")
    fields[30] = "0.0"
    q = parse_record("$".join(fields))
    assert q is not None and q["pct_chg"] == 0.0


def test_parse_message_multi_and_empty() -> None:
    payload = _SAMPLE + "#" + _SAMPLE.replace("SH600000", "SZ000001") + "#"
    quotes = parse_message(payload)                  # 两只 + 尾部空段
    assert len(quotes) == 2
    assert {q["ts_code"] for q in quotes} == {"600000.SH", "000001.SZ"}


def test_snapshot_update_get_count() -> None:
    snap = MarketSnapshot()
    assert snap.count() == 0 and snap.is_stale(10) is True      # 从未更新即陈旧
    snap.update_many(parse_message(_SAMPLE))
    assert snap.count() == 1 and snap.is_stale(60) is False
    got = snap.get("600000.SH")
    assert got is not None and got["price"] == 10.23
    got["price"] = 999                                          # 改拷贝不影响内部
    assert snap.get("600000.SH")["price"] == 10.23


def test_snapshot_to_df_columns() -> None:
    snap = MarketSnapshot()
    snap.update_many(parse_message(_SAMPLE))
    df = snap.to_df(["600000.SH"])
    assert len(df) == 1
    for col in ("ts_code", "name", "price", "pct_chg", "prev_close", "amount"):
        assert col in df.columns                     # 与 get_realtime_quote 对齐
    assert snap.to_df(["999999.SH"]).empty           # 不存在的代码 → 空


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_fullpush_parser 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
