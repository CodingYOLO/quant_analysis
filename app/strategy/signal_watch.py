"""
🔔 自选股今日信号：对自选/持仓股，判断它"近期最吃的策略信号"今天有没有触发 → 买/卖点提醒。

漂亮的复用：scout 能算出"这只票历史上最吃哪个信号"（确定性回测·非 LLM）；本模块再对**最新 K 线**
用同一信号库的 `detect()` 判断"今天是否触发"，两者合一 = "中天今天触发了它最吃的[缩量回踩MA20]买点"。
只回测今日真触发的信号（少数），比全量 scout 轻。

诚实红线（同 scout）：历史统计非预测、标样本量、小样本仅参考、不输出"必涨"、不构成买卖指令。
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import asdict

from app.backtest import strategy_scout as scout
from app.backtest.sector_backtest import _occurrences
from app.backtest.signal_backtest import _agg, _signal_defs
from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.data.kline_loader import load_kline

logger = logging.getLogger(__name__)

_WINDOW_DAYS = 90       # 历史最佳的回看窗口（同 scout 默认近3月·2024-09后风格稳定）
_BUFFER = 200           # 指标预热
_SELL_BIAS_HOT = 20.0   # 20日乖离≥此 → 过热止盈警示
_DISCLAIMER = "信号=确定性历史统计(非涨跌预测)；小样本仅参考、历史≠未来、不构成买卖指令，仓位止损自行决策。"


def scan_signals(provider: CompositeProvider | None = None, force: bool = False) -> dict:
    """对全部自选/持仓股扫今日信号。返回 {ok, date, items:[{ts_code,name,is_holding,buy,sells}], disclaimer}。"""
    from app.strategy import db
    provider = provider or CompositeProvider()
    watch = db.get_watchlist()
    if not watch:
        return {"ok": True, "items": [], "disclaimer": _DISCLAIMER, "msg": "无自选/持仓"}
    date = _latest_trade_date(provider)
    items = []
    for w in watch:
        r = _scan_one(w["ts_code"], w.get("name") or "", bool(w["is_holding"]), date, provider, force)
        if r:
            items.append(r)
    return {"ok": True, "date": date, "items": items, "disclaimer": _DISCLAIMER}


def _scan_one(ts: str, name: str, is_holding: bool, date: str,
              provider: CompositeProvider, force: bool) -> dict | None:
    if not force:
        hit = _cache_get(ts, date)
        if hit is not None:
            return hit or None          # 缓存空 {} = 今日无信号
    res = _compute(ts, name, is_holding, date, provider)
    _cache_put(ts, date, res or {})
    return res


def _compute(ts: str, name: str, is_holding: bool, date: str,
             provider: CompositeProvider) -> dict | None:
    start = _shift(date, _WINDOW_DAYS)
    buf = _shift(start, _BUFFER)
    try:
        k = load_kline(ts, buf, date, provider, adj="qfq")
    except Exception:
        logger.debug("[信号] load_kline 失败 %s", ts)
        return None
    if k is None or k.empty or len(k) < 30:
        return None
    buy = _best_firing_buy(k, start)
    sells = _sell_signals(k) if is_holding else []
    if not buy and not sells:
        return None
    return {"ts_code": ts, "name": name, "is_holding": is_holding, "buy": buy, "sells": sells}


# ──────────────────────────────────────────────────────────────────────────
# 买点：今日触发 × 历史对这票好（纯函数，可单测）
# ──────────────────────────────────────────────────────────────────────────

def _best_firing_buy(k, start: str) -> dict | None:
    """今日触发的、且历史上对这票为正期望的信号里，挑 scout 评分最高的一个。"""
    cand = []
    for key, sd in _signal_defs().items():
        if len(k) < sd["min_bars"] + 2:
            continue
        try:
            if not sd["detect"](k):          # 今日(最新K线)是否触发该信号
                continue
        except Exception:
            continue
        rets = [o["rets"][scout.HORIZON] for o in _occurrences(k, sd, start) if scout.HORIZON in o["rets"]]
        s = scout._score_signal(key, sd["label"], asdict(_agg(scout.HORIZON, rets)), scout._DEFAULT_MIN_SAMPLE)
        if s.tier in ("rec", "rec_thin"):    # 仅提醒"历史对这票为正期望"的（避免噪音）
            cand.append(s)
    if not cand:
        return None
    cand.sort(key=lambda s: (0 if s.tier == "rec" else 1, -s.score))
    b = cand[0]
    return {"signal": b.label, "category": b.category, "n": b.n,
            "win_rate": round(b.win_rate * 100), "expect": b.avg_return,
            "profit_factor": b.profit_factor, "thin": b.tier == "rec_thin"}


def _sell_signals(k) -> list[dict]:
    """持仓卖点参考：今日首次跌破MA20(趋势破位) / 乖离过热(止盈警示)。纯函数。"""
    close = k["close"]
    if len(close) < 22:
        return []
    cur, prev = float(close.iloc[-1]), float(close.iloc[-2])
    ma20 = float(close.tail(20).mean())
    ma20_prev = float(close.iloc[-21:-1].mean())
    out = []
    if cur < ma20 and prev >= ma20_prev:                 # 今日由上转下首次破位
        out.append({"signal": "今日跌破MA20(趋势破位)", "kind": "破位"})
    bias = (cur - ma20) / ma20 * 100 if ma20 else 0.0
    if bias >= _SELL_BIAS_HOT:
        out.append({"signal": f"20日乖离 +{bias:.0f}%(过热·止盈警示)", "kind": "过热"})
    return out


# ──────────────────────────────────────────────────────────────────────────
# 工具：日期 / 缓存（按 股+日 缓存·当日重复秒回）
# ──────────────────────────────────────────────────────────────────────────

def _shift(date: str, days: int) -> str:
    return (datetime.datetime.strptime(date, "%Y%m%d") - datetime.timedelta(days=days)).strftime("%Y%m%d")


def _latest_trade_date(provider: CompositeProvider) -> str:
    from app.strategy.portfolio import _latest_trade_date as _ld
    return _ld(provider)


def _cache_path(ts: str, date: str):
    d = get_settings().cache_dir / "signal_watch"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{date}__{re.sub(r'[^A-Za-z0-9.]+', '_', ts)}.json"


def _cache_get(ts: str, date: str):
    p = _cache_path(ts, date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_put(ts: str, date: str, res: dict) -> None:
    try:
        _cache_path(ts, date).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("[信号] 缓存写入失败 %s: %s", ts, e)
