"""
「走势阶段研究」：单股大周期定位 + 多周期阶段判定（纯客观结构描述·非预测/非买卖建议）。

对标"大资金看 3-5 年大周期低位优质龙头"框架：
  - 大周期定位：现价(前复权月K)在上市以来区间的历史分位 + 距历史大底/大顶——
    回答用户最关心的"这只票现在在不在大周期低位区"。
  - 阶段判定：复用 stock_profile._mtf_analysis（月线定方向·周线定节奏），不重造轮子。
  - 阶段合成：历史分位 × 月线方向 → 单一阶段标签（磨底/筑底抬升/主升/加速冲顶/高位派发/下行）。

诚实纪律：历史分位与阶段均为盘后结构描述；低位≠会涨（可更低·接飞刀·幸存者偏差），
绝不输出预测/胜率/买卖建议。前复权口径全程一致（K线与分位同源，视觉与数值对得上）。
"""

from __future__ import annotations

import datetime

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.kline_loader import load_kline
from app.strategy.stock_profile import _kline_payload, _mtf_analysis, _resample_ohlc

# 前复权全历史起点：覆盖绝大多数 A 股上市以来（load_kline 只返回实际存在的区间）。
_HISTORY_START = "20050101"
_DAILY_TAIL = 250    # 日K展示窗（约 1 年·看短期节奏）
_WEEKLY_TAIL = 200   # 周K展示窗（约 4 年·看中期节奏）
_MONTHLY_TAIL = 180  # 月K展示窗（约 15 年·尽量呈现完整大周期）
_MIN_DAILY_BARS = 250  # 少于约 1 年日K无法可靠定位大周期


def build_trend_stage(ts_code: str, name: str = "",
                      provider: CompositeProvider | None = None) -> dict:
    """
    构建单股走势阶段研究包（供 /trend 专页）。

    Args:
        ts_code: Tushare 代码，如 '600150.SH'。
        name: 股票名称（展示用，可空）。
        provider: 数据访问抽象；缺省新建 CompositeProvider（便于依赖注入/测试）。

    Returns:
        {ok, ts_code, name, bars, kline, kline_w, kline_m, cycle, mtf, stage, disclaimer}；
        数据不足时 {ok: False, msg}。
    """
    provider = provider or CompositeProvider()
    end = datetime.date.today().strftime("%Y%m%d")
    k = load_kline(ts_code, _HISTORY_START, end, provider, adj="qfq")
    if k.empty or len(k) < _MIN_DAILY_BARS:
        return {"ok": False, "ts_code": ts_code, "msg": f"{ts_code} 历史数据不足（需≥1年日K）"}

    monthly = _resample_ohlc(k, "ME")
    cycle = _cycle_position(monthly)
    mtf = _mtf_analysis(k)
    m_disp = monthly.tail(_MONTHLY_TAIL).reset_index(drop=True)   # 展示窗·分段与月K同源确保日期对齐
    return {
        "ok": True, "ts_code": ts_code, "name": name, "bars": int(len(k)),
        "kline": _kline_payload(k.tail(_DAILY_TAIL)),
        "kline_w": _kline_payload(_resample_ohlc(k, "W-FRI").tail(_WEEKLY_TAIL)),
        "kline_m": _kline_payload(m_disp),
        "segments": _segment_stages(m_disp),   # B阶段：月K全历史保守分段色带
        "cycle": cycle,
        "mtf": mtf,
        "stage": _stage_synthesis(cycle, mtf),
        "disclaimer": ("前复权口径·全为盘后结构描述：历史分位与阶段均非预测。"
                       "低位可以更低（接飞刀）、且存在幸存者偏差——不构成任何买卖建议。"),
    }


def _cycle_position(monthly: pd.DataFrame) -> dict:
    """
    大周期定位：现价（前复权月K收盘）在全历史区间的分位 + 距历史大底/大顶。

    分位 0=贴历史大底、100=贴历史大顶。用月K收盘（而非最高/最低）避免单根插针失真。
    """
    close = pd.to_numeric(monthly["close"], errors="coerce").dropna()
    if len(close) < 12:
        return {"ok": False}
    low, high, now = float(close.min()), float(close.max()), float(close.iloc[-1])
    span = high - low
    pct = (now - low) / span * 100 if span > 0 else 50.0
    return {
        "ok": True,
        "pct": round(pct, 1),
        "now": round(now, 2), "low": round(low, 2), "high": round(high, 2),
        "above_bottom": round((now / low - 1) * 100, 1) if low > 0 else 0.0,  # 高出历史大底 %
        "below_top": round((now / high - 1) * 100, 1) if high > 0 else 0.0,   # 距历史大顶 %（负=大顶下方）
        "years": round(len(close) / 12.0, 1),
        "zone": _zone_label(pct),
    }


def _zone_label(pct: float) -> str:
    """历史分位 → 通俗区间标签（纯位置描述）。"""
    if pct <= 25:
        return "大周期低位区"
    if pct <= 50:
        return "半山腰偏下"
    if pct <= 75:
        return "半山腰偏上"
    return "大周期高位区"


def _stage_synthesis(cycle: dict, mtf: dict) -> dict:
    """
    历史分位 × 月线方向 → 单一阶段标签 + 通俗说明。

    位置(分位)决定"在山的哪一段"，方向(月线)决定"正往哪走"，二者合成才是完整阶段。
    """
    if not cycle.get("ok"):
        return {"label": "—", "desc": "历史数据不足以定位大周期阶段。"}
    pct = cycle["pct"]
    mon = mtf.get("monthly", {})
    direction = mon.get("dir", "")
    above, rising, ntop = mon.get("above_ma10"), mon.get("ma10_up"), mon.get("top_count", 0)
    low, high = pct <= 30, pct >= 70

    if "见顶" in direction or (high and ntop >= 2):
        return {"label": "高位派发预警", "desc": "大周期高位 + 月线见顶信号共振——最需警惕的阶段。"}
    if high and above and rising:
        return {"label": "加速冲顶区", "desc": "已在历史高位仍加速、远离均线——利润兑现区，追高风险最大。"}
    if above and rising:
        return {"label": "主升浪", "desc": "月线站上并带动10月线上行，趋势最顺——回踩不破即持有逻辑。"}
    if low and (rising or above):
        return {"label": "筑底抬升", "desc": "低位区 + 月线开始转强——即博主说的'底部抬高'早期，需放量确认。"}
    if low:
        return {"label": "大周期磨底", "desc": "历史低位、月线尚未转强——最熬人、最无人问津，需耐心 + 基本面配合。"}
    if above is False and rising is False:
        return {"label": "中期下行", "desc": "跌破月线且10月线下行——趋势走坏，非低位不宜逆势。"}
    return {"label": "震荡待定", "desc": "方向未明——月线在均线附近反复，等突破/跌破再给方向。"}


# ── B阶段：月K全历史阶段分段（保守·机器近似·非精确断言）────────────────────────
# 设计：逐月按"月线方向×历史位置×乖离"分类 → 迟滞平滑吸收 1~2 月噪声翻转 →
# 游程合并成段，只保留 ≥ _MIN_SEG_MONTHS 的明确段；方向不明的月份留"震荡"不上色。
_MIN_SEG_MONTHS = 3     # 段最短月数（短于此视为噪声·不成段）
_MIN_PERSIST = 3        # 迟滞：新阶段需连续出现这么多月才切换
_BIAS_ACCEL = 0.30      # 冲顶乖离阈（现价高出10月线 30%+）
_LOW_PCT = 35.0         # 低位分位阈
_HIGH_PCT = 55.0        # 冲顶需同时处于的高位分位阈


def _segment_stages(m: pd.DataFrame) -> list:
    """月K全历史分段：逐月分类→迟滞平滑→合并成段。数据不足 2 年返回空（不硬分）。"""
    close = pd.to_numeric(m["close"], errors="coerce")
    dates = m["trade_date"].astype(str).tolist()
    n = len(close)
    if n < 24:
        return []
    ma10 = close.rolling(10).mean()
    gmin = float(close.min())
    span = (float(close.max()) - gmin) or 1.0
    raw = [_classify_month(i, close, ma10, gmin, span) for i in range(n)]
    return _rle_segments(dates, _smooth_labels(raw, _MIN_PERSIST), _MIN_SEG_MONTHS)


def _classify_month(i: int, close: pd.Series, ma10: pd.Series, gmin: float, span: float) -> str:
    """单月阶段分类（保守规则）。数据不足/方向不明 → '震荡'（不上色）。"""
    if i < 12 or pd.isna(ma10.iloc[i]) or pd.isna(ma10.iloc[i - 3]):
        return "震荡"
    m10 = float(ma10.iloc[i])
    if m10 <= 0:
        return "震荡"
    c = float(close.iloc[i])
    slope = m10 - float(ma10.iloc[i - 3])
    above, bias, pct = c > m10, c / m10 - 1, (c - gmin) / span * 100
    if above and slope > 0 and bias > _BIAS_ACCEL and pct >= _HIGH_PCT:
        return "加速冲顶"
    if above and slope > 0:
        return "主升"
    if (not above) and slope < 0:
        return "下行"
    if pct <= _LOW_PCT and slope > 0:
        return "筑底抬升"
    if pct <= _LOW_PCT:
        return "磨底"
    return "震荡"


def _smooth_labels(raw: list, min_persist: int) -> list:
    """迟滞平滑：新标签需连续出现 min_persist 个月才切换状态，吸收短暂噪声翻转。"""
    if not raw:
        return raw
    out, state, pending, run = [], raw[0], None, 0
    for r in raw:
        if r == state:
            pending, run = None, 0
        elif r == pending:
            run += 1
            if run >= min_persist:
                state, pending, run = r, None, 0
        else:
            pending, run = r, 1
        out.append(state)
    return out


def _rle_segments(dates: list, labels: list, min_len: int) -> list:
    """游程编码成段：合并连续相同标签，丢弃'震荡'与短于 min_len 的段。"""
    segs, i, n = [], 0, len(labels)
    while i < n:
        j = i
        while j + 1 < n and labels[j + 1] == labels[i]:
            j += 1
        if labels[i] != "震荡" and (j - i + 1) >= min_len:
            segs.append({"stage": labels[i], "start": dates[i], "end": dates[j], "months": j - i + 1})
        i = j + 1
    return segs
