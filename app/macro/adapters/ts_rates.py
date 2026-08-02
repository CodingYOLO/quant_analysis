"""L0 汇率与货币市场适配器（Tushare）。

一律走项目现有的 `CompositeProvider` → `rate_limited_call` + tenacity 重试 + parquet 缓存，
不裸调 tushare SDK（项目架构原则：所有数据访问通过 provider 抽象）。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.macro.adapters.base import Point, points_from_frame, to_ymd

logger = logging.getLogger(__name__)


def _pro():
    """取 Tushare pro api 句柄（复用 provider 的 token 与连接）。"""
    from app.data.composite_provider import CompositeProvider
    prov = CompositeProvider()
    ts_prov = getattr(prov, "_tushare", None) or getattr(prov, "tushare", None)
    api = getattr(ts_prov, "_api", None)
    if api is None:                                    # 回退：直接用配置建句柄
        import tushare as ts
        from app.config import get_settings
        api = ts.pro_api(get_settings().tushare_token)
    return api


class FxAdapter:
    """离岸人民币 USDCNH（`fx_daily`）。

    ⚠️实测坑：`fx_daily(trade_date=...)` 返回**空**，必须用 `start_date/end_date` 区间查询。
    可得代码 102 个，人民币相关只有 `USDCNH.FXCM`；**没有 ICE DXY**（美元指数另见 registry.dxy）。
    """

    name = "tushare:fx_daily"
    codes = ("usdcnh",)
    _TS_CODE = "USDCNH.FXCM"

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        df = rate_limited_call("tushare_fx_daily", _pro().fx_daily,
                               ts_code=self._TS_CODE, start_date=start, end_date=end)
        if df is None or df.empty:
            return []
        df = df.copy()
        df["_d"] = df["trade_date"].map(to_ymd)
        # 收盘价：优先 bid_close（买价收盘），缺失时退 ask_close
        col = "bid_close" if "bid_close" in df.columns else "ask_close"
        return points_from_frame(df, "_d", {col: "usdcnh"}, self.name)


class ShiborAdapter:
    """Shibor 3 个月（`shibor`，列名就是 `3m`）。"""

    name = "tushare:shibor"
    codes = ("shibor_3m",)

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        df = rate_limited_call("tushare_shibor", _pro().shibor,
                               start_date=start, end_date=end)
        if df is None or df.empty or "3m" not in df.columns:
            return []
        df = df.copy()
        df["_d"] = df["date"].map(to_ymd)
        return points_from_frame(df, "_d", {"3m": "shibor_3m"}, self.name)
