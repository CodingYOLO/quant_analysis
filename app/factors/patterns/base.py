"""
形态识别协议 + 注册表（可插拔，新增形态零侵入）。

约定：每个形态对象有 key/label/min_bars 与 detect(ohlcv)->bool。
ohlcv 为单股 DataFrame，列含 open/high/low/close/vol，按日升序，最后一行=最新交易日。
形态在「因子选股器现有不复权价格矩阵」上运行，与既有 MA/RPS/MACD 因子口径一致
（前复权仅长周期回测才关键；短周期形态选当日票，不复权足够）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class Pattern(Protocol):
    key: str            # 唯一标识，如 "breakout_high_20"
    label: str          # 显示名，如 "放量突破20日新高"
    min_bars: int       # 所需最少 K 线数

    def detect(self, ohlcv: pd.DataFrame) -> bool:
        """最后一根 K 线是否命中该形态。"""
        ...


# 名称 → 形态对象
PATTERN_REGISTRY: dict[str, Pattern] = {}


def register(p: Pattern) -> Pattern:
    """注册一个形态（重复 key 覆盖）。新增形态只需在模块内调用一次。"""
    PATTERN_REGISTRY[p.key] = p
    return p


def detect_all(ohlcv: pd.DataFrame) -> dict[str, bool]:
    """对一只股票跑全部已注册形态，返回 {key: 是否命中}。"""
    out: dict[str, bool] = {}
    for key, p in PATTERN_REGISTRY.items():
        try:
            out[key] = bool(len(ohlcv) >= p.min_bars and p.detect(ohlcv))
        except Exception:
            out[key] = False
    return out
