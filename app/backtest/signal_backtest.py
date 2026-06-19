"""
个股信号回测：选一只票 + 一个技术信号 → 历史上每次信号出现后的胜率/收益。

实用目标：回测"我看中的票 + 某策略"历史是否靠谱。
- 全程**前复权**单股序列（load_kline），无除权污染。
- **防未来函数**：信号用截至当日(含)的历史判定；买入 = 次日(T+1)开盘价；卖出 = T+N 收盘价。
- 输出：各持有期(T+1/3/5/10) 胜率/均收益/盈亏比 + 信号明细 + 资金曲线（按 T+5 复利）。

信号库 = 复用 K线形态(PATTERN_REGISTRY) + MACD/KDJ/TD九转/EMA 金叉（均可单股计算）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.kline_loader import load_kline
from app.factors import core as F
from app.factors.patterns import price_volume as _pv  # noqa: F401  触发形态注册
from app.factors.patterns.base import PATTERN_REGISTRY

logger = logging.getLogger(__name__)

HORIZONS = [1, 3, 5, 10]
_EQUITY_HORIZON = 5          # 资金曲线用 T+5 持有


# ── 信号库（单股可计算）────────────────────────────────────────────────────
def _signal_defs() -> dict[str, dict]:
    """{key: {label, min_bars, detect(ohlcv)->bool}}。复用形态 + 经典金叉信号。"""
    sig: dict[str, dict] = {}
    for k, p in PATTERN_REGISTRY.items():
        sig[k] = {"label": p.label, "min_bars": p.min_bars, "detect": p.detect}
    sig["macd_gold"] = {"label": "MACD金叉", "min_bars": 35,
                        "detect": lambda o: F.macd_golden_cross(o["close"])}
    sig["kdj_gold"] = {"label": "KDJ金叉(低位)", "min_bars": 15,
                       "detect": lambda o: F.kdj_golden_cross(o["close"], o["high"], o["low"])}
    sig["td_buy9"] = {"label": "TD神奇九转(买入9)", "min_bars": 14,
                      "detect": lambda o: F.td_buy_setup_count(o["close"]) >= 9}
    sig["ema_bull"] = {"label": "EMA14>EMA26(多头)", "min_bars": 26,
                       "detect": lambda o: F.ema_bull(o["close"])}
    return sig


def list_signals() -> list[dict]:
    """供前端下拉：[{key, label}]。"""
    return [{"key": k, "label": v["label"]} for k, v in _signal_defs().items()]


# ── 结果结构 ────────────────────────────────────────────────────────────────
@dataclass
class HorizonStat:
    horizon: int
    n: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    best: float = 0.0
    worst: float = 0.0


@dataclass
class BacktestResult:
    ts_code: str = ""
    signal: str = ""
    signal_label: str = ""
    start: str = ""
    end: str = ""
    bars: int = 0
    n_signals: int = 0
    horizons: dict = field(default_factory=dict)   # {h: HorizonStat as dict}
    trades: list = field(default_factory=list)     # 信号明细
    equity: list = field(default_factory=list)     # [{date, equity}]
    ok: bool = True
    msg: str = ""


# ── 回测主入口 ──────────────────────────────────────────────────────────────
def backtest_stock_signal(ts_code: str, signal_key: str, start: str, end: str,
                          provider: CompositeProvider | None = None) -> dict:
    """对单股单信号做历史回测，返回 dict（供 API / 前端）。"""
    defs = _signal_defs()
    sd = defs.get(signal_key)
    if not sd:
        return _err(ts_code, signal_key, "未知信号")

    provider = provider or CompositeProvider()
    k = load_kline(ts_code, start, end, provider, adj="qfq")
    if k.empty or len(k) < sd["min_bars"] + max(HORIZONS) + 2:
        return _err(ts_code, signal_key, f"{ts_code} 区间数据不足（需 ≥{sd['min_bars'] + max(HORIZONS) + 2} 根K线）")

    opens = k["open"].astype(float).tolist()
    closes = k["close"].astype(float).tolist()
    dates = k["trade_date"].astype(str).tolist()
    n = len(k)

    hret: dict[int, list[float]] = {h: [] for h in HORIZONS}
    trades, equity = [], []
    eq = 1.0

    # 遍历：i=信号日（用 0..i 历史判定，防未来函数）；买入=open[i+1]，卖出=close[i+h]
    for i in range(sd["min_bars"] - 1, n - max(HORIZONS) - 1):
        hist = k.iloc[: i + 1]
        try:
            if not sd["detect"](hist):
                continue
        except Exception:
            continue
        entry = opens[i + 1]
        if entry <= 0:
            continue
        rets = {}
        for h in HORIZONS:
            exit_p = closes[i + h]
            r = (exit_p - entry) / entry * 100 if exit_p > 0 else 0.0
            rets[h] = round(r, 2)
            hret[h].append(r)
        eq *= (1 + rets[_EQUITY_HORIZON] / 100)
        equity.append({"date": dates[i + 1], "equity": round(eq, 4)})
        trades.append({
            "signal_date": dates[i], "buy_date": dates[i + 1], "entry": round(entry, 2),
            "t1": rets[1], "t3": rets[3], "t5": rets[5], "t10": rets[10],
            "win": rets[_EQUITY_HORIZON] > 0,
        })

    res = BacktestResult(
        ts_code=ts_code, signal=signal_key, signal_label=sd["label"],
        start=dates[0], end=dates[-1], bars=n, n_signals=len(trades),
        horizons={h: _agg(h, hret[h]).__dict__ for h in HORIZONS},
        trades=trades[-60:],   # 明细只回传最近60条，避免过大
        equity=equity,
    )
    return res.__dict__


def _agg(h: int, rets: list[float]) -> HorizonStat:
    if not rets:
        return HorizonStat(horizon=h)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return HorizonStat(
        horizon=h, n=len(rets),
        win_rate=round(len(wins) / len(rets), 3),
        avg_return=round(sum(rets) / len(rets), 2),
        avg_win=round(avg_win, 2), avg_loss=round(avg_loss, 2),
        profit_factor=round(abs(sum(wins) / sum(losses)), 2) if losses and sum(losses) != 0 else 0.0,
        best=round(max(rets), 2), worst=round(min(rets), 2),
    )


def _err(ts_code: str, signal: str, msg: str) -> dict:
    return BacktestResult(ts_code=ts_code, signal=signal, ok=False, msg=msg).__dict__
