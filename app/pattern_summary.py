"""
近期走势摘要生成器。
基于最近10日日线数据（开高低收量），输出结构化文字摘要。
替代分时图分析，提供"量价形态 + 趋势判断 + 注意事项"。
"""

import pandas as pd

from app.factors import ma, volume_ratio, has_lower_shadow, pullback_quality_score


def generate_trend_summary(
    ts_code: str,
    name: str,
    close: pd.Series,
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    n_days: int = 10,
) -> str:
    """
    生成个股近 n_days 日走势摘要。

    输出结构：
    - 近期趋势（均线方向）
    - 量价配合情况
    - K线形态信号
    - 综合提示
    """
    if len(close) < n_days:
        return f"{name}（{ts_code}）：历史数据不足，无法生成摘要。"

    recent_close = close.iloc[-n_days:]
    recent_open = open_.iloc[-n_days:]
    recent_high = high.iloc[-n_days:]
    recent_low = low.iloc[-n_days:]
    recent_vol = vol.iloc[-n_days:]

    lines = [f"**{name}（{ts_code}）近{n_days}日走势摘要**"]

    # ---- 1. 趋势判断 ----
    trend_lines = _analyze_trend(close)
    lines.append(f"📈 趋势：{trend_lines}")

    # ---- 2. 量价配合 ----
    vol_lines = _analyze_volume(recent_close, recent_vol)
    lines.append(f"📊 量价：{vol_lines}")

    # ---- 3. K线形态 ----
    kline_lines = _analyze_kline_pattern(recent_close, recent_open, recent_high, recent_low, recent_vol)
    lines.append(f"🕯 形态：{kline_lines}")

    # ---- 4. 回踩质量评分 ----
    pb_score = pullback_quality_score(close, vol, open_, low)
    if pb_score >= 70:
        pb_tip = f"低吸质量评分 {pb_score:.0f}/100（高），当前位置有低吸价值"
    elif pb_score >= 50:
        pb_tip = f"低吸质量评分 {pb_score:.0f}/100（中），需等缩量缩量确认"
    else:
        pb_tip = f"低吸质量评分 {pb_score:.0f}/100（低），不建议现价买入"
    lines.append(f"🎯 买点：{pb_tip}")

    # ---- 5. 近10日涨跌幅 ----
    total_ret = (recent_close.iloc[-1] - recent_close.iloc[0]) / recent_close.iloc[0] * 100
    up_days = int((recent_close.diff().dropna() > 0).sum())
    down_days = n_days - 1 - up_days
    lines.append(f"📉 区间：近{n_days}日累计 {total_ret:+.1f}%，上涨{up_days}天/下跌{down_days}天")

    return "\n".join(lines)


def _analyze_trend(close: pd.Series) -> str:
    """分析均线趋势。"""
    parts = []

    ma5 = ma(close, 5)
    ma20 = ma(close, 20)
    ma60 = ma(close, 60) if len(close) >= 62 else None

    last = close.iloc[-1]

    if not pd.isna(ma5.iloc[-1]):
        if last > ma5.iloc[-1]:
            parts.append("站上MA5")
        else:
            parts.append("跌破MA5")

    if not pd.isna(ma20.iloc[-1]):
        dev = (last - ma20.iloc[-1]) / ma20.iloc[-1] * 100
        if last > ma20.iloc[-1]:
            parts.append(f"站上MA20（偏离+{dev:.1f}%）")
        else:
            parts.append(f"跌破MA20（偏离{dev:.1f}%）")

    if ma60 is not None and not pd.isna(ma60.iloc[-1]):
        if last > ma60.iloc[-1]:
            parts.append("站上MA60")
        else:
            parts.append("跌破MA60")

    # MA5与MA20的多空排列
    if (not pd.isna(ma5.iloc[-1])) and (not pd.isna(ma20.iloc[-1])):
        if ma5.iloc[-1] > ma20.iloc[-1]:
            parts.append("多头排列")
        else:
            parts.append("空头排列")

    return "，".join(parts) if parts else "数据不足"


def _analyze_volume(close: pd.Series, vol: pd.Series) -> str:
    """分析量价配合。"""
    if len(close) < 3:
        return "数据不足"

    parts = []
    vr = volume_ratio(vol, n=5)

    # 量比描述
    if vr > 2.0:
        parts.append(f"量比{vr:.1f}x 明显放量")
    elif vr > 1.5:
        parts.append(f"量比{vr:.1f}x 温和放量")
    elif vr < 0.7:
        parts.append(f"量比{vr:.1f}x 缩量中")
    else:
        parts.append(f"量比{vr:.1f}x 量能平稳")

    # 近3日量价配合
    price_up = close.iloc[-1] > close.iloc[-3]
    vol_up = vol.iloc[-1] > vol.iloc[-3]

    if price_up and vol_up:
        parts.append("近3日价涨量增（健康）")
    elif price_up and not vol_up:
        parts.append("近3日价涨量缩（警惕）")
    elif not price_up and vol_up:
        parts.append("近3日价跌量增（出货信号）")
    else:
        parts.append("近3日价跌量缩（筑底观察）")

    return "，".join(parts)


def _analyze_kline_pattern(
    close: pd.Series,
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
) -> str:
    """识别近期关键K线形态。"""
    signals = []

    # 最近3根K线分析
    for i in [-1, -2, -3]:
        c = close.iloc[i]
        o = open_.iloc[i]
        h = high.iloc[i]
        l = low.iloc[i]
        day_label = {-1: "昨日", -2: "前日", -3: "大前日"}.get(i, "")

        # 下影线长（支撑信号）
        if has_lower_shadow(o, l, c, min_ratio=1.0):
            body = abs(c - o)
            shadow = min(o, c) - l
            signals.append(f"{day_label}长下影线（影/实={shadow/max(body,0.01):.1f}x，支撑较强）")

        # 阳线且放量
        if c > o and i == -1:
            vr_today = vol.iloc[-1] / (vol.iloc[-6:-1].mean() + 1e-8)
            if vr_today > 1.5:
                signals.append(f"昨日放量阳线（量比{vr_today:.1f}x）")

    # 连续阴线
    if all(close.iloc[i] < open_.iloc[i] for i in [-1, -2, -3]):
        signals.append("连续3根阴线，短期弱势")

    # 连续阳线
    if all(close.iloc[i] > open_.iloc[i] for i in [-1, -2, -3]):
        signals.append("连续3根阳线，注意高位风险")

    return "，".join(signals) if signals else "近期无明显K线信号"
