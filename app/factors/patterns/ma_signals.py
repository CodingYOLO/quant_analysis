"""
短线均线信号 + 强化经典指标（阶段B）：贴合短线交易者（MA5/MA10）的看盘习惯。

每个信号判「最新一根 K 线」是否命中，复用 app.factors.core 的成熟因子。
防未来函数：仅用截至当日的历史判定（回测引擎 T+1 开盘买入、T+N 收盘卖出）。
注册即生效：本模块在 patterns/__init__.py 被导入 → 个股回测与因子选股自动出现这些信号。
"""

from __future__ import annotations

import pandas as pd

from app.factors import core as F
from .base import register


def _nan(*vals: object) -> bool:
    """任一值为 None / NaN（早期 K 线均线未成形）→ 视为不命中，避免误判。"""
    return any(v is None or pd.isna(v) for v in vals)


# ── 🅰 短线均线信号（MA5/MA10 系）────────────────────────────────────────────

class MA5CrossMA10:
    """MA5 上穿 MA10（短线金叉）：今日 MA5>MA10 且昨日 MA5≤MA10。"""

    key, label, min_bars = "ma5_cross_ma10", "MA5上穿MA10(短线金叉)", 12

    def detect(self, o: pd.DataFrame) -> bool:
        c = o["close"]
        ma5, ma10 = F.ma(c, 5), F.ma(c, 10)
        if _nan(ma5.iloc[-1], ma10.iloc[-1], ma5.iloc[-2], ma10.iloc[-2]):
            return False
        return bool(ma5.iloc[-2] <= ma10.iloc[-2] and ma5.iloc[-1] > ma10.iloc[-1])


class ShrinkPullbackMA10:
    """缩量回踩 MA10 企稳：收盘站上 MA10、盘中回踩到 MA10 附近、缩量、MA10 上行。"""

    key, label, min_bars = "shrink_pullback_ma10", "缩量回踩MA10企稳", 16

    def detect(self, o: pd.DataFrame) -> bool:
        c, low, vol = o["close"], o["low"], o["vol"]
        ma10 = F.ma(c, 10)
        if _nan(ma10.iloc[-1]):
            return False
        m = float(ma10.iloc[-1])
        above = float(c.iloc[-1]) >= m                       # 收在 MA10 上
        touched = float(low.iloc[-1]) <= m * 1.015           # 盘中回踩到 MA10 附近
        return bool(above and touched and F.is_shrink_volume(vol) and F.ma_slope(c, 10) > 0)


class MAShortBull:
    """短期均线多头：MA5>MA10>MA20 且收盘≥MA5（比 MA5>10>20>60 更早确认短线强势）。"""

    key, label, min_bars = "ma_short_bull", "短期多头(MA5>MA10>MA20)", 22

    def detect(self, o: pd.DataFrame) -> bool:
        c = o["close"]
        ma5, ma10, ma20 = (F.ma(c, n).iloc[-1] for n in (5, 10, 20))
        if _nan(ma5, ma10, ma20):
            return False
        return bool(ma5 > ma10 > ma20 and float(c.iloc[-1]) >= float(ma5))


class FirstAboveMA10:
    """首次站上 MA10：今日收盘站上 MA10、昨日还在 MA10 下方（短线由弱转强第一信号）。"""

    key, label, min_bars = "first_above_ma10", "首次站上MA10", 12

    def detect(self, o: pd.DataFrame) -> bool:
        c = o["close"]
        ma10 = F.ma(c, 10)
        if _nan(ma10.iloc[-1], ma10.iloc[-2]):
            return False
        return bool(c.iloc[-2] < ma10.iloc[-2] and c.iloc[-1] >= ma10.iloc[-1])


class Break5Recover:
    """破五反五：今日收盘收回 MA5 之上，且最近 3 日内曾收盘跌破 MA5（短线洗盘不破势·转强）。"""

    key, label, min_bars = "break5_recover", "破五反五(跌破MA5又收回)", 10

    def detect(self, o: pd.DataFrame) -> bool:
        c = o["close"]
        ma5 = F.ma(c, 5)
        if _nan(ma5.iloc[-1], ma5.iloc[-2], ma5.iloc[-3], ma5.iloc[-4]):
            return False
        recovered = float(c.iloc[-1]) >= float(ma5.iloc[-1])                       # 今日站回 MA5
        dipped = any(float(c.iloc[-i]) < float(ma5.iloc[-i]) for i in (2, 3, 4))   # 今日之前 3 日有跌破
        return bool(recovered and dipped)


class Break5RecoverVol:
    """破五反五·放量反抽：在破五反五基础上今日放量（量比≥1.5）——更接近"有效反抽"，用于对比量能是否加分。"""

    key, label, min_bars = "break5_recover_vol", "破五反五·放量反抽(量比≥1.5)", 10

    def detect(self, o: pd.DataFrame) -> bool:
        c, vol = o["close"], o["vol"]
        ma5 = F.ma(c, 5)
        if _nan(ma5.iloc[-1], ma5.iloc[-2], ma5.iloc[-3], ma5.iloc[-4]):
            return False
        recovered = float(c.iloc[-1]) >= float(ma5.iloc[-1])
        dipped = any(float(c.iloc[-i]) < float(ma5.iloc[-i]) for i in (2, 3, 4))
        return bool(recovered and dipped and F.volume_ratio(vol, 5) >= 1.5)


# ── 🅱 强化经典指标（过滤假信号）────────────────────────────────────────────

class MacdGoldAboveZero:
    """MACD 零轴上金叉：DIF 上穿 DEA 且 DIF>0（趋势中金叉，过滤弱市假金叉）。"""

    key, label, min_bars = "macd_gold_above_zero", "MACD零轴上金叉", 35

    def detect(self, o: pd.DataFrame) -> bool:
        m = F.macd(o["close"])
        dif, dea = m["dif"], m["dea"]
        if _nan(dif.iloc[-1], dea.iloc[-1], dif.iloc[-2], dea.iloc[-2]):
            return False
        cross = dif.iloc[-2] < dea.iloc[-2] and dif.iloc[-1] >= dea.iloc[-1]
        return bool(cross and dif.iloc[-1] > 0)


class RsiOversoldRecover:
    """RSI 超卖回升：RSI(14) 由 <30 上穿 30（超跌反弹，周期/有色尤其灵）。"""

    key, label, min_bars = "rsi_oversold_recover", "RSI超卖回升(<30上穿)", 16

    def detect(self, o: pd.DataFrame) -> bool:
        r = F.rsi(o["close"], 14)
        if _nan(r.iloc[-1], r.iloc[-2]):
            return False
        return bool(r.iloc[-2] < 30 and r.iloc[-1] >= 30)


class BigYangVolume:
    """放量大阳线：涨幅≥阈值、收阳、放量（量比≥1.5）——资金强势启动的直接信号。"""

    def __init__(self, gain_min: float = 6.0, vol_mult: float = 1.5):
        self.gain_min, self.vol_mult = gain_min, vol_mult
        self.key = "big_yang_volume"
        self.label = f"放量大阳线(≥{gain_min:g}%)"
        self.min_bars = 7

    def detect(self, o: pd.DataFrame) -> bool:
        c, op, vol = o["close"], o["open"], o["vol"]
        if _nan(c.iloc[-1], c.iloc[-2]):
            return False
        gain = (float(c.iloc[-1]) - float(c.iloc[-2])) / (float(c.iloc[-2]) + 1e-8) * 100
        yang = float(c.iloc[-1]) > float(op.iloc[-1])
        return bool(gain >= self.gain_min and yang and F.volume_ratio(vol, 5) >= self.vol_mult)


# 注册（新增信号只需在此 register；个股回测 + 因子选股自动出现）
for _p in (
    MA5CrossMA10(), ShrinkPullbackMA10(), MAShortBull(), FirstAboveMA10(),
    Break5Recover(), Break5RecoverVol(),
    MacdGoldAboveZero(), RsiOversoldRecover(), BigYangVolume(),
):
    register(_p)
