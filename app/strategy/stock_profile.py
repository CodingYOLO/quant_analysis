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
from app.strategy.key_levels import build_key_levels


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
    chips = _chips(ts_code, provider, k)
    return {
        "ok": True, "ts_code": ts_code, "name": name, "bars": len(k),
        "metrics": metrics,
        "tags": _tags(metrics),
        "hints": _form_hints(k),
        "kline": _kline_payload(k.tail(120)),
        "kline_w": _kline_payload(_resample_ohlc(k, "W-FRI").tail(120)),   # 周线(约2年)
        "kline_m": _kline_payload(_resample_ohlc(k, "ME").tail(48)),       # 月线(约4年·够10月线)
        "mtf": _mtf_analysis(k),                                          # 多周期判定(月线定方向·周线定节奏)
        "chips": chips,
        "levels": build_key_levels(k, chips),
    }


# ── 筹码分布（主力成本/获利盘/成本密集区，Tushare cyq_perf）──────────────────
def _chips(ts_code: str, provider: CompositeProvider, k: pd.DataFrame) -> dict | None:
    """最新筹码分布快照 + 解读。日期严格对齐：用筹码数据自身日期的收盘价算溢价。"""
    import datetime
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=40)).strftime("%Y%m%d")
    try:
        df = provider.get_cyq_perf(ts_code, start, end)
    except Exception as e:
        logger.debug("[chips] %s 筹码获取失败: %s", ts_code, e)
        return None
    if df is None or df.empty:
        return None

    df = df.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    row = df.sort_values("trade_date").iloc[-1]
    chip_date = row["trade_date"]

    def num(key):
        v = pd.to_numeric(row.get(key), errors="coerce")
        return float(v) if pd.notna(v) else None

    weight_avg = num("weight_avg")
    winner = num("winner_rate")
    c5, c50, c95 = num("cost_5pct"), num("cost_50pct"), num("cost_95pct")

    # 溢价：严格用「筹码数据同日收盘价」对比平均成本（日期对齐，保证准确）
    kd = k[k["trade_date"].astype(str) == chip_date]
    ref_close = float(kd["close"].iloc[-1]) if not kd.empty else float(k["close"].iloc[-1])
    premium = round((ref_close - weight_avg) / weight_avg * 100, 1) if weight_avg else None
    concentration = round((c95 - c5) / c50 * 100, 1) if (c5 and c50 and c95) else None

    return {
        "date": chip_date, "ref_close": round(ref_close, 2),
        "weight_avg": round(weight_avg, 2) if weight_avg else None,
        "winner_rate": round(winner, 1) if winner is not None else None,
        "cost_5pct": round(c5, 2) if c5 else None,
        "cost_50pct": round(c50, 2) if c50 else None,
        "cost_95pct": round(c95, 2) if c95 else None,
        "premium": premium, "concentration": concentration,
        "tags": _chip_tags(premium, winner, concentration),
    }


def _chip_tags(premium, winner, concentration) -> list[dict]:
    tags = []
    if premium is not None:
        if premium > 15:
            tags.append({"text": f"现价高于平均成本 {premium:+.0f}%（远离主力成本·追高风险）", "level": "warn"})
        elif premium < -5:
            tags.append({"text": f"现价低于平均成本 {premium:+.0f}%（跌破主力成本·偏弱或低吸位）", "level": "calm"})
        else:
            tags.append({"text": f"现价处主力成本上方 {premium:+.0f}%（结构健康）", "level": "info"})
    if winner is not None:
        if winner >= 85:
            tags.append({"text": f"获利盘 {winner:.0f}%（普遍获利·警惕高位抛压）", "level": "warn"})
        elif winner < 30:
            tags.append({"text": f"获利盘仅 {winner:.0f}%（套牢盘重·上方有压力）", "level": "warn"})
        else:
            tags.append({"text": f"获利盘 {winner:.0f}%（多空相对均衡）", "level": "info"})
    if concentration is not None:
        if concentration <= 15:
            tags.append({"text": f"筹码高度集中（90%成本区跨度 {concentration:.0f}%·分歧小）", "level": "calm"})
        elif concentration >= 40:
            tags.append({"text": f"筹码分散（跨度 {concentration:.0f}%·成本分歧大）", "level": "info"})
    return tags


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

    def _ma(n: int) -> list:
        return [round(x, 2) if pd.notna(x) else None for x in close.rolling(n).mean()]

    return {
        "dates": k["trade_date"].astype(str).tolist(),
        # ECharts 蜡烛图顺序：[open, close, low, high]
        "candle": [[round(float(o), 2), round(float(c), 2), round(float(l), 2), round(float(h), 2)]
                   for o, c, l, h in zip(k["open"], k["close"], k["low"], k["high"])],
        "vol": [int(v) for v in k["vol"]],
        "ma5": _ma(5), "ma10": _ma(10), "ma20": _ma(20), "ma60": _ma(60),
    }


# ── 多周期（周线/月线）：大周期决定方向·小周期决定节奏 ─────────────────────────────
def _resample_ohlc(k: pd.DataFrame, rule: str) -> pd.DataFrame:
    """日K → 周(W-FRI)/月(ME) OHLCV。open=区间首·high=区间高·low=区间低·close=区间末·vol=区间和。"""
    df = k.copy()
    df["_dt"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
    agg = (df.set_index("_dt")
             .resample(rule)
             .agg({"open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum"})
             .dropna(subset=["close"]).reset_index())
    agg["trade_date"] = agg["_dt"].dt.strftime("%Y%m%d")
    return agg


def _monthly_vol_stall(m: pd.DataFrame) -> bool:
    """月线「放巨量不涨」近似：最近一根月K量为近12月最大，且当月涨幅乏力(<3%)——冲高滞涨(顶部特征之一)。"""
    if len(m) < 12:
        return False
    v = pd.to_numeric(m["vol"], errors="coerce")
    c = pd.to_numeric(m["close"], errors="coerce")
    last_vol_max = float(v.iloc[-1]) >= float(v.tail(12).max()) * 0.98
    mret = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100 if len(c) >= 2 and c.iloc[-2] else 0.0
    return bool(last_vol_max and mret < 3.0)


def _mtf_analysis(k: pd.DataFrame) -> dict:
    """多周期趋势判定（纯描述·非买卖建议）：月线定方向(+见顶三条件) · 周线定节奏 · synthesis。

    对标博主框架：大周期决定方向、小周期决定节奏；月线主升浪未破前，日线回踩=低吸机会而非清仓。
    """
    from app.factors.core import macd as _macd
    out: dict = {}
    m = _resample_ohlc(k, "ME")
    mc = pd.to_numeric(m["close"], errors="coerce")
    if len(mc) >= 12:
        ma10 = mc.rolling(10).mean()
        ma10_now, ma10_ref = float(ma10.iloc[-1]), float(ma10.iloc[-4])   # 10月线 vs 3月前(判拐头)
        close_now = float(mc.iloc[-1])
        ma10_up, above = ma10_now > ma10_ref, close_now > ma10_now
        md = _macd(mc)
        dif, dea = md["dif"], md["dea"]
        macd_dead = bool(len(dif) >= 2 and dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2])
        tops = {"10月线拐头下+跌破": bool((not ma10_up) and (not above)),
                "月线MACD死叉": macd_dead,
                "放巨量不涨": _monthly_vol_stall(m)}
        ntop = sum(tops.values())
        if above and ma10_up:
            direction = "主升浪·月线强势向上" if close_now > ma10_now * 1.08 else "月线向上·趋势健康"
        elif ntop >= 2:
            direction = "⚠️月线见顶预警(三条件≥2共振)"
        elif (not above) and (not ma10_up):
            direction = "月线走坏·中期趋势破位"
        else:
            direction = "月线震荡·方向待定"
        out["monthly"] = {"dir": direction, "close": round(close_now, 2), "ma10": round(ma10_now, 2),
                          "above_ma10": above, "ma10_up": ma10_up, "top_signals": tops, "top_count": ntop,
                          "bars": int(len(mc))}
    w = _resample_ohlc(k, "W-FRI")
    wc = pd.to_numeric(w["close"], errors="coerce")
    if len(wc) >= 8:
        ma5w = wc.rolling(5).mean()
        dev = (float(wc.iloc[-1]) / float(ma5w.iloc[-1]) - 1) * 100 if pd.notna(ma5w.iloc[-1]) and ma5w.iloc[-1] else 0.0
        if dev > 8:
            rhythm = "周线加速·偏离均值(短期波段高位·宜控仓/兑现部分)"
        elif dev < -5:
            rhythm = "周线回踩·靠近均值(月线向上时=低吸窗口)"
        else:
            rhythm = "周线均衡·节奏平稳"
        out["weekly"] = {"rhythm": rhythm, "dev_ma5w": round(dev, 1)}
    md = out.get("monthly", {}).get("dir", "—")
    wr = out.get("weekly", {}).get("rhythm", "—")
    out["summary"] = (f"月线定方向：{md} ｜ 周线定节奏：{wr} ｜ 日线找买点(见下方股性/K线)。"
                      "顺大势逆小势——月线未走坏，日线回踩是低吸而非清仓；月线见顶三条件共振才是真离场信号。")
    out["disclaimer"] = "多周期为盘后结构描述·非买卖建议；月线见顶三条件为近似观察(10月线拐头跌破/月MACD死叉/放量滞涨)。"
    return out
