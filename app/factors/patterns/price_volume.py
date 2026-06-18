"""
量价形态库（阶段A）：确定性强、A股最实用。新增形态在本文件 register 即生效。

每个形态接收单股 OHLCV（DataFrame，列 open/high/low/close/vol，按日升序），
判断「最新一根 K 线」是否命中。复用 app.factors.core 的成熟因子，避免重复造轮子。
"""

from __future__ import annotations

import pandas as pd

from app.factors import core as F
from .base import register


class BreakoutPriorHigh:
    """放量突破 N 日新高：收盘创不含今日的前 N 日新高，且今日量 ≥ vol_mult×近5日均量。"""

    def __init__(self, n: int = 20, vol_mult: float = 1.5):
        self.n, self.vol_mult = n, vol_mult
        self.key = f"breakout_high_{n}"
        self.label = f"放量突破{n}日新高"
        self.min_bars = n + 5

    def detect(self, o: pd.DataFrame) -> bool:
        close, vol = o["close"], o["vol"]
        prior_high = close.iloc[-(self.n + 1):-1].max()      # 不含今日的前 N 日最高收盘
        breakout = close.iloc[-1] > prior_high
        volume_up = F.volume_ratio(vol, n=5) >= self.vol_mult
        return bool(breakout and volume_up)


class ShrinkPullbackMA20:
    """缩量回踩MA20企稳：站上MA20且回踩幅度<3%、缩量、下影线、MA20上行（复用回踩质量评分）。"""

    key, label, min_bars = "shrink_pullback_ma20", "缩量回踩MA20企稳", 25

    def detect(self, o: pd.DataFrame) -> bool:
        score = F.pullback_quality_score(o["close"], o["vol"], o["open"], o["low"])
        return bool(score >= 60)


class MABullStack:
    """均线多头排列：MA5 > MA10 > MA20 > MA60 且 收盘 ≥ MA5（标准强势结构）。"""

    key, label, min_bars = "ma_bull_stack", "均线多头排列", 60

    def detect(self, o: pd.DataFrame) -> bool:
        c = o["close"]
        ma5, ma10, ma20, ma60 = (float(c.tail(n).mean()) for n in (5, 10, 20, 60))
        return bool(ma5 > ma10 > ma20 > ma60 and c.iloc[-1] >= ma5)


class PlatformBreakout:
    """平台突破：今日收盘突破前 N 日箱体上沿，且突破前箱体窄幅震荡（波幅 < 阈值）+ 放量。"""

    def __init__(self, n: int = 15, amp_max: float = 0.12, vol_mult: float = 1.3):
        self.n, self.amp_max, self.vol_mult = n, amp_max, vol_mult
        self.key, self.label, self.min_bars = "platform_breakout", "平台突破", n + 6

    def detect(self, o: pd.DataFrame) -> bool:
        close, high, low, vol = o["close"], o["high"], o["low"], o["vol"]
        box_high = high.iloc[-(self.n + 1):-1]
        box_low = low.iloc[-(self.n + 1):-1]
        box_top = box_high.max()
        box_amp = (box_high.max() - box_low.min()) / (box_low.min() + 1e-8)  # 箱体振幅
        narrow = box_amp <= self.amp_max                      # 突破前窄幅整理
        breakout = close.iloc[-1] > box_top                   # 突破箱体上沿
        volume_up = F.volume_ratio(vol, n=5) >= self.vol_mult
        return bool(narrow and breakout and volume_up)


class VolPriceSurge:
    """量价齐升：今日收涨、放量(量比≥1.5)、站上MA5（短线资金进场的量价共振）。"""

    key, label, min_bars = "vol_price_surge", "量价齐升", 10

    def detect(self, o: pd.DataFrame) -> bool:
        close, vol = o["close"], o["vol"]
        up = close.iloc[-1] > close.iloc[-2]
        volume_up = F.volume_ratio(vol, n=5) >= 1.5
        above_ma5 = close.iloc[-1] >= float(close.tail(5).mean())
        return bool(up and volume_up and above_ma5)


# 注册（新增形态只需在此 register；screener 自动出现该筛选项）
for _p in (
    BreakoutPriorHigh(n=20),
    ShrinkPullbackMA20(),
    MABullStack(),
    PlatformBreakout(),
    VolPriceSurge(),
):
    register(_p)
