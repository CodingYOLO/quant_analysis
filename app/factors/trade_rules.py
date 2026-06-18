"""
交易规则文本生成（对标吴川：多条件止损 + 次日验证条件量化）。

纯函数，输入已算好的因子值，输出结构化、可勾选的中文规则文本。
不读数据源、不预测涨跌，只把"硬规则"翻译成人类可执行的清单。
"""

from __future__ import annotations

# 多条件止损参数（配置化）
_VOL_BREAK_MULT = 1.5     # 放量下跌阈值：今日量 > N × 近20日均量 且收阴
_FLOW_NEG_YI = -5.0       # 资金1日转负超过 N 亿 视为出逃
# 次日验证参数
_OPEN_FLOOR_PCT = 2.0     # 开盘价不低于昨收的 -N%
_VOL_SHRINK_PCT = 30.0    # 分时量能较昨日同期萎缩不超过 N%


def build_stop_rule(stop_price: float, theme: str = "", ma20: float = 0.0) -> str:
    """
    多条件止损（个股级 + 题材级），对标吴川：
      跌破止损位/MA20 / 放量下跌(量>1.5×20日均量且收阴) / 资金1日转负超-5亿 → 离场；
      题材级：所属板块资金连续3日净流出 或 板块龙头破位 → 同步减仓。

    Args:
        stop_price: 个股级止损位（价格，跌破即离场）。
        theme:      所属板块名（题材级止损用）。
        ma20:       20日均线价（可选，给出则附注 MA20 参考位）。
    """
    level = f"{stop_price:.2f}" if stop_price and stop_price > 0 else "止损位"
    ma20_note = f"（结构位：20日均线 {ma20:.2f}）" if ma20 and ma20 > 0 else ""
    stock_level = (
        f"个股级（满足任一即离场）：① 收盘跌破止损位 {level}{ma20_note}；"
        f"② 放量下跌（成交量 > {_VOL_BREAK_MULT:.1f}×近20日均量 且收阴线）；"
        f"③ 当日主力资金净流出超 {abs(_FLOW_NEG_YI):.0f} 亿"
    )
    theme_part = theme or "所属板块"
    theme_level = f"题材级：{theme_part} 资金连续3日净流出 或 板块龙头破位 → 同步减仓"
    return f"{stock_level}。{theme_level}。"


def build_nextday_checklist(close: float) -> list[str]:
    """
    次日 09:30-09:40 开盘验证清单（量化、可勾选），对标吴川 4 条：
      1) 开盘价 ≥ 昨收×(1-2%)；2) 分时量能较昨日同期萎缩不超30%；
      3) 当日主力资金维持净流入（正）；4) 不出现低开低走补跌形态。
    满足才介入，否则放弃当日。

    Args:
        close: 选股日收盘价（昨收），用于算开盘下限价。
    """
    floor_price = round(close * (1 - _OPEN_FLOOR_PCT / 100), 2) if close and close > 0 else None
    open_rule = (
        f"开盘价不低于昨收 {close:.2f} 的 -{_OPEN_FLOOR_PCT:.0f}%（即 ≥ {floor_price}）"
        if floor_price else f"开盘价不低于昨收的 -{_OPEN_FLOOR_PCT:.0f}%"
    )
    return [
        open_rule,
        f"分时量能较昨日同期萎缩不超过 {_VOL_SHRINK_PCT:.0f}%",
        "当日主力资金维持净流入（1日资金为正）",
        "不出现低开低走的补跌形态",
    ]
