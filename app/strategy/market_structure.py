"""大盘/板块结构事件引擎（2026-08-03 用户定档：诊断页的病根=只给存量排名不给增量变化）。

三样输出，全部**盘后结构描述·未回测·非买卖建议**：
  index_radar      七大指数 × 日/周/月 三周期结构灯板（均线排列/金叉死叉/月线形态）
  fresh_cross 等   纯函数事件检测：金叉死叉(三种口径分开标)、月线阳包阴/阴包阳、连阳连阴
  事件抽取         只报**最新一根K线上新发生**的事件——"今天什么变了"，不是"谁一直很强"

口径纪律：
· 死叉不是一个词而是三种：MA5×MA10(短线)、MA10×MA20(中线)、MACD DIF×DEA——分开标注，
  混为一谈会让"死叉"失去信息量；
· 月线形态在**本月未收官**时必须带"未收官·收官前可变"标注（半根K线的形态会变）；
· 全部为描述档（未经回测），与诊断页既有可信度分层一致——只有回测过的才许叫"信号"。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.cache import cached_daily
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

# 七大核心指数（覆盖大盘/成长/小盘三种口径——用户点名要科创/上证的结构）
INDEXES = [
    ("000001.SH", "上证指数"), ("399001.SZ", "深证成指"), ("000300.SH", "沪深300"),
    ("399006.SZ", "创业板指"), ("000688.SH", "科创50"), ("000905.SH", "中证500"),
    ("399303.SZ", "国证2000"),
]


# ──────────────────────────────────────────────
# 纯函数事件检测（可单测）
# ──────────────────────────────────────────────

def fresh_cross(fast: pd.Series, slow: pd.Series) -> str:
    """最新一根K线上是否**新发生**交叉：'gold'|'dead'|''。前一根在另一侧才算"新"。"""
    if len(fast) < 2 or len(slow) < 2:
        return ""
    f0, f1 = fast.iloc[-2], fast.iloc[-1]
    s0, s1 = slow.iloc[-2], slow.iloc[-1]
    if pd.isna(f0) or pd.isna(f1) or pd.isna(s0) or pd.isna(s1):
        return ""
    if f1 > s1 and f0 <= s0:
        return "gold"
    if f1 < s1 and f0 >= s0:
        return "dead"
    return ""


def engulfing(o_prev: float, c_prev: float, o_cur: float, c_cur: float) -> str:
    """K线吞没形态：'阳包阴'|'阴包阳'|''。实体完全覆盖前一根实体（常用宽松口径）。"""
    if any(pd.isna(x) for x in (o_prev, c_prev, o_cur, c_cur)):
        return ""
    prev_yin, prev_yang = c_prev < o_prev, c_prev > o_prev
    cur_yang, cur_yin = c_cur > o_cur, c_cur < o_cur
    if cur_yang and prev_yin and c_cur >= o_prev and o_cur <= c_prev:
        return "阳包阴"
    if cur_yin and prev_yang and c_cur <= o_prev and o_cur >= c_prev:
        return "阴包阳"
    return ""


def streak(closes: pd.Series, opens: pd.Series, n_max: int = 6) -> str:
    """连阳/连阴计数（≥3才报·上限n_max根）：'连3阳'|'连4阴'|''。"""
    if len(closes) < 3:
        return ""
    sign = None
    cnt = 0
    for c, o in zip(closes.iloc[::-1], opens.iloc[::-1]):
        if pd.isna(c) or pd.isna(o) or c == o:
            break
        s = 1 if c > o else -1
        if sign is None:
            sign = s
        if s != sign:
            break
        cnt += 1
        if cnt >= n_max:
            break
    if cnt < 3:
        return ""
    return f"连{cnt}{'阳' if sign == 1 else '阴'}"


def month_open(end: str, provider: CompositeProvider | None = None) -> bool:
    """end 之后本月是否还有交易日（有=本月K线未收官）。日历失败按未收官处理（保守）。"""
    try:
        from app.macro.sync import trading_days
        month_end = (pd.Timestamp(end) + pd.offsets.MonthEnd(0)).strftime("%Y%m%d")
        nxt = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y%m%d")
        if nxt > month_end:
            return False
        return len(trading_days(nxt, month_end)) > 0
    except Exception:
        return True


def _macd(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    from app.factors.core import macd as _m
    md = _m(close)
    return md["dif"], md["dea"]


# ──────────────────────────────────────────────
# 单标的结构行（指数与板块共用）
# ──────────────────────────────────────────────

def structure_row(k: pd.DataFrame, name: str, unfinished_month: bool) -> dict:
    """日/周/月三周期结构 + 新事件列表。k: trade_date/open/high/low/close 升序日线。"""
    from app.strategy.stock_profile import _resample_ohlc
    c = pd.to_numeric(k["close"], errors="coerce")
    out: dict = {"name": name, "events": []}

    # ── 日线：均线排列 + 三种交叉（分开标·混称"死叉"会失去信息量）──
    ma5, ma10, ma20, ma60 = (c.rolling(w).mean() for w in (5, 10, 20, 60))
    if pd.notna(ma20.iloc[-1]):
        align = ("多头排列" if ma5.iloc[-1] > ma10.iloc[-1] > ma20.iloc[-1]
                 else "空头排列" if ma5.iloc[-1] < ma10.iloc[-1] < ma20.iloc[-1] else "缠绕")
        out["daily"] = {
            "align": align,
            "above_ma20": bool(c.iloc[-1] > ma20.iloc[-1]),
            "above_ma60": bool(pd.notna(ma60.iloc[-1]) and c.iloc[-1] > ma60.iloc[-1]),
        }
        dif, dea = _macd(c)
        for cr, label in ((fresh_cross(ma5, ma10), "MA5×10(短线)"),
                          (fresh_cross(ma10, ma20), "MA10×20(中线)"),
                          (fresh_cross(dif, dea), "MACD")):
            if cr:
                word = "金叉" if cr == "gold" else "死叉"
                out["daily"][f"x_{label}"] = word
                out["events"].append({"level": "日线", "name": name,
                                      "event": f"日线{label}新{word}", "dir": cr})

    # ── 周线：10周线上下 + 周MACD 新交叉 + 新破位/新站上 ──
    w = _resample_ohlc(k, "W-FRI")
    wc = pd.to_numeric(w["close"], errors="coerce")
    if len(wc) >= 12:
        ma10w = wc.rolling(10).mean()
        above = bool(pd.notna(ma10w.iloc[-1]) and wc.iloc[-1] > ma10w.iloc[-1])
        out["weekly"] = {"above_ma10w": above}
        cr_ma = fresh_cross(wc, ma10w)          # 收盘对10周线的穿越=新站上/新破位
        if cr_ma == "gold":
            out["events"].append({"level": "周线", "name": name, "event": "新站上10周线", "dir": "gold"})
            out["weekly"]["x_ma10w"] = "新站上"
        elif cr_ma == "dead":
            out["events"].append({"level": "周线", "name": name, "event": "跌破10周线", "dir": "dead"})
            out["weekly"]["x_ma10w"] = "新跌破"
        difw, deaw = _macd(wc)
        cr_w = fresh_cross(difw, deaw)
        if cr_w:
            word = "金叉" if cr_w == "gold" else "死叉"
            out["weekly"]["x_macd"] = word
            out["events"].append({"level": "周线", "name": name,
                                  "event": f"周MACD新{word}", "dir": cr_w})

    # ── 月线：方向(10月线) + 吞没形态 + 连阳连阴 ──
    m = _resample_ohlc(k, "ME")
    mo, mc = pd.to_numeric(m["open"], errors="coerce"), pd.to_numeric(m["close"], errors="coerce")
    if len(mc) >= 12:
        ma10m = mc.rolling(10).mean()
        out["monthly"] = {
            "above_ma10m": bool(pd.notna(ma10m.iloc[-1]) and mc.iloc[-1] > ma10m.iloc[-1]),
            "ma10m_up": bool(pd.notna(ma10m.iloc[-4]) and ma10m.iloc[-1] > ma10m.iloc[-4]),
        }
        pat = engulfing(mo.iloc[-2], mc.iloc[-2], mo.iloc[-1], mc.iloc[-1])
        tagm = "(本月未收官·收官前可变)" if unfinished_month else "(已收官)"
        if pat:
            out["monthly"]["pattern"] = pat + tagm
            out["events"].append({"level": "月线", "name": name,
                                  "event": f"月线{pat}{tagm}",
                                  "dir": "gold" if pat == "阳包阴" else "dead"})
        # 上月收官形态：吞没只在收官当天是"最新一根"——新月一开、7月的阴包阳就会从
        # 事件流消失，而那恰是最该被看到的时点。补救：形态常驻状态列；新月前3个交易日
        # 内仍作为事件播报（之后只留状态·避免同一事件连报20天）
        if unfinished_month and len(mc) >= 3:
            pat_closed = engulfing(mo.iloc[-3], mc.iloc[-3], mo.iloc[-2], mc.iloc[-2])
            if pat_closed:
                out["monthly"]["pattern_closed"] = pat_closed + "(上月收官)"
                last_month = str(k["trade_date"].iloc[-1])[:6]
                days_in_cur = int((k["trade_date"].astype(str).str[:6] == last_month).sum())
                if days_in_cur <= 3:
                    out["events"].append({"level": "月线", "name": name,
                                          "event": f"上月收官·月线{pat_closed}",
                                          "dir": "gold" if pat_closed == "阳包阴" else "dead"})
        stk = streak(mc, mo)
        if stk:
            out["monthly"]["streak"] = stk
    return out


# ──────────────────────────────────────────────
# 指数结构雷达（日缓存）
# ──────────────────────────────────────────────

def index_radar(end: str, provider: CompositeProvider | None = None) -> dict:
    """七大指数三周期结构灯板 + 指数级新事件。"""
    def _build():
        prov = provider or CompositeProvider()
        start = (pd.Timestamp(end) - pd.Timedelta(days=1250)).strftime("%Y%m%d")
        unfinished = month_open(end, prov)
        rows, events = [], []
        for code, name in INDEXES:
            try:
                k = prov.get_index_daily_range(code, start, end)
                if k is None or len(k) < 260:
                    continue
                k = k.sort_values("trade_date").reset_index(drop=True)
                r = structure_row(k, name, unfinished)
                r["code"] = code
                events += r.pop("events")
                rows.append(r)
            except Exception as e:
                logger.warning("[结构雷达] %s 失败: %s", name, e)
        sev = {"月线": 0, "周线": 1, "日线": 2}
        events.sort(key=lambda e: sev.get(e["level"], 9))
        return pd.DataFrame([{"payload": __import__("json").dumps(
            {"rows": rows, "events": events, "unfinished_month": unfinished},
            ensure_ascii=False)}])
    df = cached_daily("index_structure_v2", end, _build)
    import json
    out = json.loads(df["payload"].iloc[0])
    out.update(ok=True, end=end,
               note="三种交叉分开标注(MA5×10短线/MA10×20中线/MACD)；月线形态未收官必标注。"
                    "盘后结构描述·未回测·非买卖建议。")
    return out
