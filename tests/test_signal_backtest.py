"""
个股信号回测单测：前复权连续性 + 防未来函数 + 收益计算正确。

零依赖（用假 Provider 喂合成数据），可直接运行：python -m tests.test_signal_backtest
"""

from __future__ import annotations

import pandas as pd

from app.data.kline_loader import apply_qfq
from app.backtest.signal_backtest import backtest_stock_signal, _agg, _custom_signal_def


# ── 前复权纯函数 ──
def test_apply_qfq_continuous() -> None:
    # 第3天除权（因子从2.0→1.0），原始价在除权日跳水；前复权后应连续
    daily = pd.DataFrame({
        "trade_date": ["20240101", "20240102", "20240103", "20240104"],
        "open": [20, 20, 10, 10], "high": [21, 21, 11, 11],
        "low": [19, 19, 9, 9], "close": [20, 20, 10, 10], "vol": [1, 1, 1, 1],
    })
    adj = pd.DataFrame({"trade_date": ["20240101", "20240102", "20240103", "20240104"],
                        "adj_factor": [2.0, 2.0, 1.0, 1.0]})
    q = apply_qfq(daily, adj)
    # 前复权：前两天 ×(2/1)=×2 → 40，后两天 ×1 → 10... 等等，最新因子=1.0
    # ratio = factor/latest(1.0)：前两天=2.0→×2=40，后两天=1.0→×1=10
    assert q["close"].tolist() == [40.0, 40.0, 10.0, 10.0]   # 历史被放大，消除跳水


# ── 聚合统计纯函数 ──
def test_agg_stats() -> None:
    s = _agg(5, [10.0, -5.0, 20.0, -10.0])   # 2胜2负
    assert s.n == 4 and s.win_rate == 0.5
    assert s.avg_return == 3.75 and s.best == 20.0 and s.worst == -10.0
    assert s.profit_factor == 2.0            # 盈30/亏15
    assert _agg(5, []).n == 0                # 空安全


# ── 假 Provider 整库回测：验证防未来函数 + 收益口径 ──
class _FakeProvider:
    """构造一段 close 单调上涨的序列；信号每天命中（用恒真信号验证撮合口径）。"""
    def __init__(self, n=60):
        dates = [f"202401{i:02d}" if i < 32 else f"202402{i-31:02d}" for i in range(1, n + 1)]
        self._df = pd.DataFrame({
            "trade_date": dates,
            "open": [100 + i for i in range(n)], "high": [101 + i for i in range(n)],
            "low": [99 + i for i in range(n)], "close": [100.5 + i for i in range(n)],
            "vol": [1000] * n, "amount": [1e5] * n, "pct_chg": [1.0] * n,
        })

    def get_stock_daily(self, ts_code, start, end):
        return self._df

    def get_adj_factor_series(self, ts_code, start, end):
        return pd.DataFrame({"trade_date": self._df["trade_date"], "adj_factor": [1.0] * len(self._df)})


def test_backtest_no_future_leak_and_returns() -> None:
    # 均线多头排列在持续上涨序列上恒成立 → 每天有信号；验证 entry=次日open，exit=close[i+h]
    r = backtest_stock_signal("TEST.SZ", "ma_bull_stack", "20240101", "20240301",
                              provider=_FakeProvider(n=80))
    assert r["ok"] and r["n_signals"] > 0
    # 上涨序列 → 所有持有期应全胜
    assert r["horizons"][5]["win_rate"] == 1.0
    assert r["horizons"][1]["win_rate"] == 1.0
    # 资金曲线单调上升（持续盈利）
    eq = [e["equity"] for e in r["equity"]]
    assert eq == sorted(eq) and eq[-1] > 1.0
    # 明细字段齐全
    t = r["trades"][-1]
    assert {"signal_date", "buy_date", "entry", "t1", "t5", "win"} <= set(t.keys())


def test_custom_signal_def() -> None:
    # 当日跌3~7%买：detect 看最后一根 pct_chg
    sd = _custom_signal_def({"pct_min": -7, "pct_max": -3})
    assert "当日涨跌 -7~-3%" in sd["label"]
    o_hit = pd.DataFrame({"pct_chg": [1, 2, -5], "vol": [1000, 1000, 1000]})
    o_miss = pd.DataFrame({"pct_chg": [1, 2, -1], "vol": [1000, 1000, 1000]})
    assert sd["detect"](o_hit) is True
    assert sd["detect"](o_miss) is False     # 跌1%不在区间

    # 叠加放量：量比需≥1.5
    sd2 = _custom_signal_def({"pct_min": -7, "pct_max": -3, "vol_mode": "up"})
    assert "放量" in sd2["label"]
    o_vol = pd.DataFrame({"pct_chg": [1] * 5 + [-5], "vol": [1000] * 5 + [3000]})   # 放量
    o_novol = pd.DataFrame({"pct_chg": [1] * 5 + [-5], "vol": [1000] * 6})           # 不放量
    assert sd2["detect"](o_vol) is True
    assert sd2["detect"](o_novol) is False


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
