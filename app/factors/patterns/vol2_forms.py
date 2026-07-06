"""VOL.2 进阶篇形态：跳空突破缺口 / 均线粘合(蓄能) / 二次金叉 / 阴线反包。

借鉴《A股实战研判手册·进阶篇》——把老口诀恢复成立条件后量化。判断「最新一根 K 线」是否命中。
定位：**"帮你快速找到符合这些结构的票"的过滤器·非涨跌预测**（形态类前瞻IC多为弱/看regime）。
单股 OHLCV（open/high/low/close/vol·按日升序·最后一行=最新）。复用 app.factors.core。
"""

from __future__ import annotations

import pandas as pd

from app.factors import core as F
from .base import register


class GapBreakout:
    """跳空突破缺口：今日向上跳空(今低>昨高) + 放量≥2倍 + 收盘破前N日平台上沿 + 突破前窄幅
    (排除高位衰竭缺口)。对标 VOL.2「突破缺口」——缺口上沿此后变支撑。"""

    def __init__(self, n: int = 20, vol_mult: float = 2.0, amp_max: float = 0.25):
        self.n, self.vol_mult, self.amp_max = n, vol_mult, amp_max
        self.key, self.label, self.min_bars = "gap_breakout", "跳空突破缺口(放量2倍·平台边缘)", n + 6

    def detect(self, o: pd.DataFrame) -> bool:
        close, high, low, vol = o["close"], o["high"], o["low"], o["vol"]
        gap_up = o["open"].iloc[-1] > high.iloc[-2]                 # 跳空高开：今日开盘 > 昨日最高
        box_high = high.iloc[-(self.n + 1):-1]
        box_low = low.iloc[-(self.n + 1):-1]
        breakout = close.iloc[-1] > box_high.max()                 # 收盘破前 N 日高
        box_amp = (box_high.max() - box_low.min()) / (box_low.min() + 1e-8)
        narrow = box_amp <= self.amp_max                           # 前期窄幅=平台边缘(排高位衰竭)
        vol_up = F.volume_ratio(vol, n=5) >= self.vol_mult
        return bool(gap_up and breakout and narrow and vol_up)


class MAGlue:
    """均线粘合(蓄能待发)：MA5/MA10/MA20 三线最大间距<2%·且价未跌破粘合带(非破位)。
    对标 VOL.2「均线粘合」——蓄能·方向未定的埋伏区(不是买卖信号·等放量发散确认方向)。"""

    def __init__(self, spread_max: float = 0.02):
        self.spread_max = spread_max
        self.key, self.label, self.min_bars = "ma_glue", "均线粘合(MA5/10/20间距<2%·蓄能待发)", 25

    def detect(self, o: pd.DataFrame) -> bool:
        c = o["close"]
        ma5, ma10, ma20 = (float(c.tail(n).mean()) for n in (5, 10, 20))
        mmax, mmin = max(ma5, ma10, ma20), min(ma5, ma10, ma20)
        glue = (mmax - mmin) / (mmin + 1e-8) <= self.spread_max
        healthy = c.iloc[-1] >= mmin * 0.98                        # 价没跌破粘合带=非破位下行
        return bool(glue and healthy)


class MASecondCross:
    """二次金叉：今日 MA5 上穿 MA10，且近 look 日内已发生过一次金叉+随后死叉(洗盘)。
    对标 VOL.2「二次金叉」——第一次金叉套人洗盘·第二次才是真启动·可靠性高于首次。"""

    def __init__(self, look: int = 25):
        self.look = look
        self.key, self.label, self.min_bars = "ma_second_cross", "二次金叉(MA5二次上穿MA10·洗盘后)", look + 14

    def detect(self, o: pd.DataFrame) -> bool:
        c = o["close"]
        diff = (c.rolling(5).mean() - c.rolling(10).mean()).dropna()
        if len(diff) < self.look + 2:
            return False
        today_cross = diff.iloc[-2] <= 0 and diff.iloc[-1] > 0     # 今日金叉
        if not today_cross:
            return False
        sign = (diff.iloc[-(self.look + 1):-1] > 0).astype(int)    # 近 look 日(不含今日)
        crosses_up = int((sign.diff() == 1).sum())                 # 之前金叉次数
        crosses_dn = int((sign.diff() == -1).sum())                # 之前死叉次数
        return bool(crosses_up >= 1 and crosses_dn >= 1)           # 曾金叉→死叉洗盘→今二次金叉


class YinReclaim:
    """阴线反包：前 1 日放量大阴(实体跌≥yin_drop%) → 今日阳线收盘站上该阴线开盘价 + 量≥阴线80%
    + 站上 MA20(趋势中·非下降反抽)。对标 VOL.2「反包」——次日实体反包=洗盘确认。"""

    def __init__(self, yin_drop: float = 3.0):
        self.yin_drop = yin_drop
        self.key, self.label, self.min_bars = "yin_reclaim", "阴线反包(放量大阴后次日实体反包)", 25

    def detect(self, o: pd.DataFrame) -> bool:
        op, close, vol = o["open"], o["close"], o["vol"]
        y_open, y_close, y_vol = float(op.iloc[-2]), float(close.iloc[-2]), float(vol.iloc[-2])
        yin = y_close < y_open and (y_open - y_close) / (y_open + 1e-8) * 100 >= self.yin_drop
        t_open, t_close, t_vol = float(op.iloc[-1]), float(close.iloc[-1]), float(vol.iloc[-1])
        reclaim = t_close > t_open and t_close > y_open            # 今阳且收盘站上阴线开盘价(实体反包)
        vol_ok = t_vol >= y_vol * 0.8
        in_trend = t_close >= float(close.tail(20).mean())         # 站上MA20=上升趋势中(简化)
        return bool(yin and reclaim and vol_ok and in_trend)


# 注册（新增形态只需在此 register；screener 自动出现 pat_* 筛选项）
for _p in (GapBreakout(), MAGlue(), MASecondCross(), YinReclaim()):
    register(_p)
