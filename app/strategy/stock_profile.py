"""
个股「股性画像」：从前复权日K自算这只票的脾气，帮你快速懂它、少踩坑。

全部指标自洽于单股 OHLCV（load_kline），不依赖外部资金/财报数据。
产出：量化指标 + 通俗标签 + 当前K线形态提示 + 近120日K线(供前端画图)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.kline_loader import load_kline
from app.factors import core as F
from app.nodes.quick_report import _board_limit_pct


def build_stock_profile(ts_code: str, name: str = "",
                        provider: CompositeProvider | None = None,
                        lookback_days: int = 600) -> dict:
    """
    构建股性画像。lookback_days 取约 2.5 年日K。
    返回 {ok, ts_code, bars, metrics, tags, hints, kline}。
    """
    provider = provider or CompositeProvider()
    import datetime
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=int(lookback_days * 1.6))).strftime("%Y%m%d")
    k = load_kline(ts_code, start, end, provider, adj="qfq")
    if k.empty or len(k) < 60:
        return {"ok": False, "ts_code": ts_code, "msg": f"{ts_code} 历史数据不足"}

    k = k.tail(lookback_days).reset_index(drop=True)
    metrics = _metrics(k, ts_code, name)
    return {
        "ok": True, "ts_code": ts_code, "name": name, "bars": len(k),
        "metrics": metrics,
        "tags": _tags(metrics),
        "hints": _form_hints(k),
        "kline": _kline_payload(k.tail(120)),
    }


# ── 量化指标 ────────────────────────────────────────────────────────────────
def _metrics(k: pd.DataFrame, ts_code: str, name: str) -> dict:
    close = k["close"].astype(float)
    high = k["high"].astype(float)
    low = k["low"].astype(float)
    pct = pd.to_numeric(k["pct_chg"], errors="coerce").fillna(0.0)
    prev = close.shift(1)

    ret = close.pct_change().dropna()
    amplitude = ((high - low) / prev.replace(0, np.nan)).dropna() * 100
    vol_annual = float(ret.std() * np.sqrt(252) * 100)

    lim = _board_limit_pct(ts_code, name)
    up_lim = pct >= (lim - 0.3)
    dn_lim = pct <= -(lim - 0.3)
    # 最高连板
    max_board, cur = 0, 0
    for u in up_lim:
        cur = cur + 1 if u else 0
        max_board = max(max_board, cur)

    # 近一年（约242交易日）窗口的涨跌停次数
    win = min(len(k), 242)
    up1y = int(up_lim.tail(win).sum())
    dn1y = int(dn_lim.tail(win).sum())

    ma20 = close.rolling(20).mean()
    above20_ratio = float((close > ma20).tail(len(k) - 20).mean() * 100) if len(k) > 20 else 0.0

    # 最大回撤
    cummax = close.cummax()
    max_dd = float(((close - cummax) / cummax).min() * 100)

    # 追高友好度：大涨(>5%)次日平均涨跌
    big = pct > 5
    nxt = pct.shift(-1)
    chase = float(nxt[big].mean()) if big.sum() >= 3 else None

    up_days = ret[ret > 0]
    dn_days = ret[ret < 0]
    return {
        "amplitude_avg": round(float(amplitude.tail(win).mean()), 2),
        "volatility_annual": round(vol_annual, 1),
        "limit_up_1y": up1y, "limit_down_1y": dn1y, "max_board": max_board,
        "above_ma20_ratio": round(above20_ratio, 1),
        "max_drawdown": round(max_dd, 1),
        "chase_nextday": round(chase, 2) if chase is not None else None,
        "up_day_ratio": round(float((ret > 0).mean() * 100), 1),
        "avg_up": round(float(up_days.mean() * 100), 2) if len(up_days) else 0.0,
        "avg_down": round(float(dn_days.mean() * 100), 2) if len(dn_days) else 0.0,
    }


# ── 通俗标签 ────────────────────────────────────────────────────────────────
def _tags(m: dict) -> list[dict]:
    """生成股性标签 [{text, level}]，level: hot(红)/warn(橙)/calm(绿)/info(灰)。"""
    tags = []
    v = m["volatility_annual"]
    tags.append({"text": f"年化波动 {v:.0f}%（{'高波动·短线属性' if v >= 45 else ('中波动' if v >= 28 else '低波动·稳健')}）",
                 "level": "hot" if v >= 45 else ("warn" if v >= 28 else "calm")})

    spec = m["limit_up_1y"] >= 12 or m["max_board"] >= 3
    tags.append({"text": f"近1年涨停 {m['limit_up_1y']} 次·最高 {m['max_board']} 连板（{'有妖性·游资活跃' if spec else '少涨停·机构属性'}）",
                 "level": "warn" if spec else "info"})

    r = m["above_ma20_ratio"]
    tags.append({"text": f"站上MA20时间占比 {r:.0f}%（{'趋势性强·适合持有' if r >= 55 else ('多空均衡' if r >= 45 else '偏弱·震荡为主')}）",
                 "level": "calm" if r >= 55 else ("info" if r >= 45 else "warn")})

    c = m["chase_nextday"]
    if c is not None:
        tags.append({"text": f"大涨(>5%)次日均 {c:+.2f}%（{'追高友好·有惯性' if c > 0.3 else ('追高谨慎·易高开低走' if c < -0.3 else '追高中性')}）",
                     "level": "calm" if c > 0.3 else ("warn" if c < -0.3 else "info")})

    tags.append({"text": f"历史最大回撤 {m['max_drawdown']:.0f}%（{'回撤大·控仓位' if m['max_drawdown'] <= -45 else '回撤可控'}）",
                 "level": "warn" if m["max_drawdown"] <= -45 else "info"})
    return tags


# ── 当前K线形态提示（规则，确定性，无LLM）───────────────────────────────────
def _form_hints(k: pd.DataFrame) -> list[str]:
    close = k["close"].astype(float)
    vol = k["vol"].astype(float)
    cur = float(close.iloc[-1])
    ma5, ma10, ma20, ma60 = (float(close.tail(n).mean()) for n in (5, 10, 20, 60))
    hints = []

    # 均线结构
    if ma5 > ma10 > ma20 > ma60:
        hints.append("✅ 均线多头排列（MA5>10>20>60），趋势向上")
    elif ma5 < ma10 < ma20 < ma60:
        hints.append("⛔ 均线空头排列，趋势向下，不宜抄底")
    hints.append(("✅ 站上MA20 " if cur >= ma20 else "⚠️ 跌破MA20 ") + f"(MA20={ma20:.2f})，" +
                 ("结构健康" if cur >= ma20 else "短期转弱"))

    # 近期涨幅过热
    if len(close) >= 8:
        ret7 = (cur / float(close.iloc[-8]) - 1) * 100
        if ret7 >= 20:
            hints.append(f"🔴 近7日已涨 {ret7:+.1f}%，短期过热，追高风险大")
        elif ret7 <= -15:
            hints.append(f"🟢 近7日跌 {ret7:+.1f}%，超跌，留意企稳反弹")

    # 量能
    vr = F.volume_ratio(vol, n=5)
    if vr >= 1.8:
        hints.append(f"📈 今日明显放量（量比{vr:.1f}），资金活跃")
    elif vr <= 0.7:
        hints.append(f"📉 今日缩量（量比{vr:.1f}），观望情绪浓")

    # 距60日高低点位置
    hi60, lo60 = float(close.tail(60).max()), float(close.tail(60).min())
    pos = (cur - lo60) / (hi60 - lo60 + 1e-8) * 100
    if pos >= 90:
        hints.append(f"⚠️ 处于60日高位区（{pos:.0f}%分位），接近压力")
    elif pos <= 15:
        hints.append(f"💡 处于60日低位区（{pos:.0f}%分位），靠近支撑")
    return hints


# ── K线数据（供前端 ECharts 画蜡烛图）───────────────────────────────────────
def _kline_payload(k: pd.DataFrame) -> dict:
    close = k["close"].astype(float)
    return {
        "dates": k["trade_date"].astype(str).tolist(),
        # ECharts 蜡烛图顺序：[open, close, low, high]
        "candle": [[round(float(o), 2), round(float(c), 2), round(float(l), 2), round(float(h), 2)]
                   for o, c, l, h in zip(k["open"], k["close"], k["low"], k["high"])],
        "vol": [int(v) for v in k["vol"]],
        "ma5": [round(x, 2) if pd.notna(x) else None for x in close.rolling(5).mean()],
        "ma20": [round(x, 2) if pd.notna(x) else None for x in close.rolling(20).mean()],
        "ma60": [round(x, 2) if pd.notna(x) else None for x in close.rolling(60).mean()],
    }
