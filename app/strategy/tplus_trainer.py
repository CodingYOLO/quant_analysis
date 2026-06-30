"""做T训练（盘中 T+0 波段·临场练习）：选股+近期某交易日，分时图逐步播放(看不到未来)，
你高抛低吸，收盘结算"做T相对不动多赚多少 / 摊低成本多少"。数据=新浪免费分钟线(近~5个交易日)。

A股 T+1 规则下的"做T"：手里有底仓 N 股，日内卖出一部分(高抛)、跌了再买回(低吸)，
日终股数回到 N，口袋里多出价差现金 → 摊低成本。引擎只做**结算与校验**(纯函数·可测)，
回放与下单在前端(分时逐根揭示·临场感)。

诚实：分时为真实历史分钟线；结算如实对比"全程不动"，做反(追涨杀跌)就显示亏。
"""

from __future__ import annotations

import datetime
import logging
import random

logger = logging.getLogger(__name__)

_MIN_BARS = 60          # 一天分时至少这么多根才出题（过滤停牌/半天等异常）


def _sina_symbol(ts_code: str) -> str:
    """600519.SH→sh600519 / 000001.SZ→sz000001。"""
    code, _, mkt = ts_code.partition(".")
    return ("sh" if mkt == "SH" else "sz" if mkt == "SZ" else "bj") + code


def fetch_minute(ts_code: str, *, scale: int = 1, datalen: int = 1200) -> list[dict]:
    """新浪免费分钟线 → [{time,'open','high','low','close','vol'}]（升序·失败回 []）。"""
    import httpx
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={_sina_symbol(ts_code)}&scale={scale}&ma=no&datalen={datalen}")
    try:
        data = httpx.get(url, timeout=15.0).json()
    except Exception as e:
        logger.warning("[做T] 新浪分时取数失败 %s: %s", ts_code, e)
        return []
    out = []
    for b in (data or []):
        try:
            out.append({"time": b["day"], "open": float(b["open"]), "high": float(b["high"]),
                        "low": float(b["low"]), "close": float(b["close"]), "vol": float(b["volume"])})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _vwap_line(bars: list[dict]) -> list[float]:
    """分时均价线（累计成交额/累计量·分时图标配·判断现价强弱用）。"""
    cum_amt = cum_vol = 0.0
    out = []
    for b in bars:
        cum_amt += b["close"] * b["vol"]
        cum_vol += b["vol"]
        out.append(round(cum_amt / cum_vol, 3) if cum_vol else b["close"])
    return out


def settle(prices: list[float], trades: list[dict], *, base: int, close: float,
           prev_close: float) -> dict:
    """做T结算（纯函数·可测）。

    Args:
        prices: 当日分时各根的成交价（结算按 trade['i'] 取价）。
        trades: [{i: bar下标, side: 'sell'/'buy', qty: 股数}]，按发生顺序。
        base: 底仓股数。close: 当日收盘价。prev_close: 昨收（算"不动"基准）。

    **T+1 铁律（A股）**：今天买入的当天不能卖。所以一天内**最多只能卖出原有底仓 base 那么多**——
    卖出量受 `base − 已卖` 约束（卖的永远是老股）；今天买回/买入的那部分**锁定到次日**、不计入可卖。
    买入量约束到 `base − 已买`（买回或正T加仓·最多到底仓规模）。越界的那笔按可成交量截断。
    支持反T(先卖后买)与正T(先买后卖)，两者在 T+1 下都成立。

    Returns: 现金/期末持仓/做T超额(相对不动)/摊低成本/卖买均价/笔数/做对做反 评语。
    """
    sold = bought = 0.0          # 当日累计卖/买（T+1：卖受 base-sold 限，今买 bought 锁定不可卖）
    sell_amt = buy_amt = sell_qty = buy_qty = 0.0
    n_sell = n_buy = 0
    for t in trades:
        i = int(t.get("i", -1))
        if not (0 <= i < len(prices)):
            continue
        p = prices[i]
        side = t.get("side")
        q = float(t.get("qty") or 0)
        if q <= 0:
            continue
        if side == "sell":
            q = min(q, base - sold)                  # T+1：只能卖原底仓·已卖的不再卖·今买的锁定
            if q <= 0:
                continue
            sold += q
            sell_amt += q * p
            sell_qty += q
            n_sell += 1
        elif side == "buy":
            q = min(q, base - bought)                # 买回/正T加仓·最多到底仓规模
            if q <= 0:
                continue
            bought += q
            buy_amt += q * p
            buy_qty += q
            n_buy += 1

    holding = base - sold + bought                  # 期末持仓 = 底仓 − 卖 + 买
    cash = round(sell_amt - buy_amt, 2)              # 做T实现现金（卖收-买付）
    # 总财富对比"全程不动"：做T = 现金 + 期末持仓市值；不动 = 一直持 base
    excess = round(cash + (holding - base) * close, 2)
    per_share = round(excess / base, 4) if base else 0.0
    sell_vwap = round(sell_amt / sell_qty, 3) if sell_qty else None
    buy_vwap = round(buy_amt / buy_qty, 3) if buy_qty else None
    hold_pnl = round(base * (close - prev_close), 2)  # 不动当日盈亏（昨收→收盘）

    if n_sell == 0 and n_buy == 0:
        verdict = "没做T（全程没动）"
    elif excess > 0:
        verdict = f"做T成功·相对不动多赚 {excess:.0f} 元（摊低成本 {per_share:.3f} 元/股）"
    elif excess < 0:
        verdict = f"做T做亏了·相对不动少 {abs(excess):.0f} 元（高买低卖/追涨杀跌了）"
    else:
        verdict = "做T打平"

    return {
        "n_sell": n_sell, "n_buy": n_buy,
        "sell_qty": int(sell_qty), "buy_qty": int(buy_qty),
        "sell_vwap": sell_vwap, "buy_vwap": buy_vwap,
        "cash": cash, "end_holding": int(holding), "restored": holding == base,
        "excess": excess, "per_share": per_share,
        "hold_pnl": hold_pnl, "verdict": verdict,
    }


def _prev_close(provider, ts_code: str, day: str) -> float | None:
    """昨收：day 之前最近一个交易日的收盘（分时图基准线）。容错回 None。"""
    try:
        from app.data.kline_loader import load_kline
        d = datetime.date(int(day[:4]), int(day[5:7]), int(day[8:10]))
        start = (d - datetime.timedelta(days=20)).strftime("%Y%m%d")
        kl = load_kline(ts_code, start, day.replace("-", ""), provider, adj="none")
        prev = kl[kl["trade_date"].astype(str) < day.replace("-", "")]
        return round(float(prev["close"].iloc[-1]), 3) if len(prev) else None
    except Exception as e:
        logger.debug("[做T] 昨收取数失败: %s", e)
        return None


def build_session(provider=None, *, code: str | None = None, day: str | None = None) -> dict:
    """开一局做T：选股 + 某交易日 → 当日真实分时(逐根·含均价) + 昨收 + 可选日期。"""
    if provider is None:
        from app.data.composite_provider import CompositeProvider
        provider = CompositeProvider()
    from app.strategy.perception_trainer import _pick_weighted, _universe

    if code:
        ts = code
        name = next((n for c, n, _i in _universe(provider) if c == code), code[:6])
    else:
        ts, name, _ind = _pick_weighted(provider)        # AI/科技为主（与盘感同口径）

    bars = fetch_minute(ts, scale=1, datalen=1200)
    if not bars:
        return {"ok": False, "msg": "分时取数失败，请重试或换只票"}
    by_day: dict[str, list] = {}
    for b in bars:
        by_day.setdefault(b["time"][:10], []).append(b)
    full_days = sorted(d for d, v in by_day.items() if len(v) >= _MIN_BARS)
    if not full_days:
        return {"ok": False, "msg": "该票近期无完整分时（新浪免费仅近~5个交易日）"}
    sel = day if (day and day in by_day and len(by_day[day]) >= _MIN_BARS) else random.choice(full_days)
    if day and sel != day:
        return {"ok": False, "msg": "该日不在免费分时回溯范围（仅近~5个交易日）"}

    db = by_day[sel]
    vwap = _vwap_line(db)
    prev_close = _prev_close(provider, ts, sel)
    chart = [{"t": b["time"][11:16], "o": round(b["open"], 3), "h": round(b["high"], 3),
              "l": round(b["low"], 3), "c": round(b["close"], 3), "v": round(b["vol"]),
              "avg": vwap[k]} for k, b in enumerate(db)]
    return {"ok": True, "ts_code": ts, "name": name, "day": sel,
            "prev_close": prev_close, "bars": chart, "avail_days": full_days}
