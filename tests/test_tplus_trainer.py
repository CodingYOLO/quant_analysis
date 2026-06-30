"""做T训练——结算核心 settle 单测（纯函数·不连网）。"""

from __future__ import annotations

from app.strategy.tplus_trainer import _sina_symbol, settle


def test_symbol_convert() -> None:
    assert _sina_symbol("600519.SH") == "sh600519"
    assert _sina_symbol("000001.SZ") == "sz000001"


def test_no_trades() -> None:
    r = settle([10, 11], [], base=1000, close=11, prev_close=10)
    assert r["verdict"].startswith("没做T") and r["excess"] == 0 and r["end_holding"] == 1000


def test_high_sell_low_buy_profit() -> None:
    # 第0根价12卖500，第2根价10买回500 → 现金 500*(12-10)=1000·还原底仓
    r = settle([12, 11, 10, 11], [{"i": 0, "side": "sell", "qty": 500},
                                  {"i": 2, "side": "buy", "qty": 500}],
               base=1000, close=11, prev_close=11)
    assert r["cash"] == 1000 and r["restored"] and r["excess"] == 1000 and r["per_share"] == 1.0
    assert "成功" in r["verdict"] and r["sell_vwap"] == 12 and r["buy_vwap"] == 10


def test_wrong_way_loss() -> None:
    # 低卖高买（追涨杀跌）：第0根10卖，第1根12买回 → 现金 500*(10-12)=-1000
    r = settle([10, 12], [{"i": 0, "side": "sell", "qty": 500},
                          {"i": 1, "side": "buy", "qty": 500}],
               base=1000, close=12, prev_close=10)
    assert r["cash"] == -1000 and r["excess"] == -1000 and "亏" in r["verdict"]


def test_sell_clamped_to_holding() -> None:
    r = settle([10, 10], [{"i": 0, "side": "sell", "qty": 5000}], base=1000, close=10, prev_close=10)
    assert r["sell_qty"] == 1000 and r["end_holding"] == 0       # 卖超持仓被截断到底仓


def test_buy_first_allowed_zhengT() -> None:
    # 正T：起手买入(老底仓不动)→允许·持仓升到1500·今买锁定到明天
    r = settle([10, 10], [{"i": 0, "side": "buy", "qty": 500}], base=1000, close=10, prev_close=10)
    assert r["buy_qty"] == 500 and r["end_holding"] == 1500


def test_t1_cannot_resell_todays_buy() -> None:
    """T+1 铁律：卖500(可卖剩500)→买回500(今买·锁定)→再想卖1000，只能再卖剩下的老股500，
    一天累计卖出绝不超过原底仓1000。旧的错误模型会卖到1500(把今买的也卖了)。"""
    r = settle([10, 10, 10, 10],
               [{"i": 0, "side": "sell", "qty": 500},
                {"i": 1, "side": "buy", "qty": 500},
                {"i": 2, "side": "sell", "qty": 1000}],
               base=1000, close=10, prev_close=10)
    assert r["sell_qty"] == 1000          # 累计只卖了原底仓1000(不是1500)
    assert r["end_holding"] == 500        # 1000 - 1000(卖) + 500(买)


def test_partial_not_restored_marks_at_close() -> None:
    # 卖500没买回·期末持仓500：excess = 现金5000 + (500-1000)*收盘9 = 500
    r = settle([10, 9], [{"i": 0, "side": "sell", "qty": 500}], base=1000, close=9, prev_close=10)
    assert r["cash"] == 5000 and r["end_holding"] == 500 and r["restored"] is False
    assert r["excess"] == 500


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_tplus_trainer 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
