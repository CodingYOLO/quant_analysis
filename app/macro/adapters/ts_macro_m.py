"""L0 月频宏观适配器（Tushare）。

**point-in-time 关键**：月频数据的 `as_of` 一律取**报告期月末**，配合 `metric_meta.lag_days`
由 `sync` 决定"哪个交易日起才可见"。绝不能让 1 月的 CPI 出现在 1 月的面板上——
那是前视偏差，会让回看模式给出当时根本不可能知道的结论。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.macro.adapters.base import Point, month_end, points_from_frame

logger = logging.getLogger(__name__)


def _pro():
    from app.macro.adapters.ts_rates import _pro as p
    return p()


class _MonthlyBase:
    """月频适配器公共骨架：拉月度宽表 → 按 {列: code} 展开，as_of = 报告期月末。"""

    name = ""
    codes: tuple[str, ...] = ()
    api_name = ""
    month_col = "month"
    cols: dict[str, str] = {}
    scale: dict[str, float] = {}

    def _call(self, start_m: str, end_m: str):
        from app.data.cache import rate_limited_call
        return rate_limited_call(f"tushare_{self.api_name}", getattr(_pro(), self.api_name),
                                 start_m=start_m, end_m=end_m)

    def fetch(self, start: str, end: str) -> list[Point]:
        df = self._call(start[:6], end[:6])
        if df is None or df.empty or self.month_col not in df.columns:
            return []
        df = df.copy()
        df["_d"] = df[self.month_col].map(month_end)
        return points_from_frame(df, "_d", self.cols, self.name, self.scale)


class MoneySupplyAdapter(_MonthlyBase):
    """M1/M2 同比（`cn_m`）。M1 对A股的领先性优于 M2。"""
    name = "tushare:cn_m"
    api_name = "cn_m"
    codes = ("m1_yoy", "m2_yoy")
    cols = {"m1_yoy": "m1_yoy", "m2_yoy": "m2_yoy"}


class SocialFinanceAdapter(_MonthlyBase):
    """社融增量（`sf_month`）。

    ⚠️原需求写的接口名 `cn_sf` **不存在**（实测报"请指定正确的接口名"），正确是 `sf_month`。
    列：month / inc_month(当月增量) / inc_cumval(累计) / stk_endval(存量)。
    """
    name = "tushare:sf_month"
    api_name = "sf_month"
    codes = ("social_finance_inc",)
    cols = {"inc_month": "social_finance_inc"}


class CpiAdapter(_MonthlyBase):
    """CPI 同比（`cn_cpi`，用全国口径 nt_yoy）。"""
    name = "tushare:cn_cpi"
    api_name = "cn_cpi"
    codes = ("cpi_yoy",)
    cols = {"nt_yoy": "cpi_yoy"}


class PpiAdapter(_MonthlyBase):
    """PPI 同比（`cn_ppi`）。"""
    name = "tushare:cn_ppi"
    api_name = "cn_ppi"
    codes = ("ppi_yoy",)
    cols = {"ppi_yoy": "ppi_yoy"}


class PmiAdapter(_MonthlyBase):
    """制造业 PMI（`cn_pmi`）。

    ⚠️两个与其它月频接口不一样的地方，都踩过：
      ① 月份列名是大写 **`MONTH`**（`cn_cpi`/`cn_m`/`sf_month` 都是小写 `month`）；
      ② 共 65 列且列名是**指标编码**而非可读名。编码规律：PMI01xxxx=制造业、
         PMI02xxxx=非制造业、PMI03xxxx=综合产出指数；全零后缀为该族的总指数，
         故制造业 PMI = `PMI010000`（实测均值 49.79、区间[49.0, 50.4]，与荣枯线附近的常识一致）。
    编码不存在时 `points_from_frame` 会告警并跳过（留空，绝不改猜别的列）。
    """
    name = "tushare:cn_pmi"
    api_name = "cn_pmi"
    codes = ("pmi_mfg",)
    month_col = "MONTH"
    cols = {"PMI010000": "pmi_mfg"}


class LprAdapter:
    """LPR 1年 / 5年以上（`shibor_lpr`）。日频发布但每月20日才变，按日频取、当日频处理。"""

    name = "tushare:shibor_lpr"
    codes = ("lpr_1y", "lpr_5y")

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        from app.macro.adapters.base import to_ymd
        df = rate_limited_call("tushare_shibor_lpr", _pro().shibor_lpr,
                               start_date=start, end_date=end)
        if df is None or df.empty:
            return []
        df = df.copy()
        df["_d"] = df["date"].map(to_ymd)
        return points_from_frame(df, "_d", {"1y": "lpr_1y", "5y": "lpr_5y"}, self.name)
