"""适配器注册表。

新增指标 = 写一个适配器类 + 在 `ADAPTERS` 登记 + 在 `registry.METRICS` 定义元数据。
**计算层与前端不需要任何改动**——这是配置驱动的关键。
"""

from __future__ import annotations

from app.macro.adapters.ak_rates import BondYieldAdapter, RepoRateAdapter
from app.macro.adapters.base import (Adapter, MacroFetchError, MissingSourceError,
                                     Point, StaleDataError, TruncatedError)
from app.macro.adapters.ts_macro_m import (CpiAdapter, LprAdapter, MoneySupplyAdapter,
                                           PmiAdapter, PpiAdapter, SocialFinanceAdapter)
from app.macro.adapters.ts_flow import (EtfShareAdapter, FloatReleaseAdapter,
                                        MainflowAdapter, MarginAdapter,
                                        NewFundAdapter, NorthboundAdapter,
                                        TurnoverAdapter)
from app.macro.adapters.noaa_oni import OniAdapter
from app.macro.adapters.ts_fut import FuturesMainAdapter
from app.macro.adapters.ts_rates import FxAdapter, ShiborAdapter

# 已接入：L0(commit 2) + L1(commit 4) + IND(领先指标页·2026-08-03 + ONI·2026-08-04)。
# L2/L3 后续追加。
ADAPTERS: tuple[Adapter, ...] = (
    FuturesMainAdapter(),
    OniAdapter(),
    BondYieldAdapter(),
    RepoRateAdapter(),
    FxAdapter(),
    ShiborAdapter(),
    MoneySupplyAdapter(),
    SocialFinanceAdapter(),
    CpiAdapter(),
    PpiAdapter(),
    PmiAdapter(),
    LprAdapter(),
    TurnoverAdapter(),
    MarginAdapter(),
    NorthboundAdapter(),
    MainflowAdapter(),
    EtfShareAdapter(),
    NewFundAdapter(),
    FloatReleaseAdapter(),
)


def for_codes(codes: set[str]) -> list[Adapter]:
    """返回能提供这批 code 中任意一个的适配器（去重、保持登记顺序）。"""
    return [a for a in ADAPTERS if set(a.codes) & codes]


def covered_codes() -> set[str]:
    """所有适配器能提供的 code 全集——供自检"启用的指标是否都有适配器"。"""
    return {c for a in ADAPTERS for c in a.codes}


__all__ = ["ADAPTERS", "Adapter", "Point", "for_codes", "covered_codes",
           "MacroFetchError", "StaleDataError", "MissingSourceError", "TruncatedError"]
