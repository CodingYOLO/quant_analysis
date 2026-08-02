"""L0 利率类适配器（akshare 境内源）。

覆盖两个 Tushare 拿不到的核心指标：
  · 中债/美债收益率曲线 —— Tushare `yc_cb` 在本账号 5100 分**无权限**
  · FDR007 回购利率     —— 免掉中国货币网爬虫（最脆弱的部件）
"""

from __future__ import annotations

import logging

import pandas as pd

from app.macro.adapters.base import Point, points_from_frame, sliced_fetch, to_ymd

logger = logging.getLogger(__name__)


class BondYieldAdapter:
    """中债 + 美债收益率曲线，一个接口拿全（`ak.bond_zh_us_rate`）。

    实测：2023-01-03 起，中债10Y 非空 892 行，覆盖 750 交易日回补窗口绰绰有余。
    返回列含 中国国债收益率 2/5/10/30年 与 美国国债收益率 2/5/10/30年。
    """

    name = "akshare:bond_zh_us_rate"
    codes = ("cn_10y", "us_10y", "us_2y", "cn_term_spread_10y2y", "cn_us_spread_10y")

    _COLS = {
        "中国国债收益率10年": "cn_10y",
        "美国国债收益率10年": "us_10y",
        "美国国债收益率2年": "us_2y",
    }

    def fetch(self, start: str, end: str) -> list[Point]:
        import akshare as ak
        df = ak.bond_zh_us_rate(start_date=start)
        if df is None or df.empty:
            return []
        df = df.copy()
        df["_d"] = df["日期"].map(to_ymd)
        df = df[(df["_d"] >= start) & (df["_d"] <= end)]
        pts = points_from_frame(df, "_d", self._COLS, self.name)

        # 派生：期限利差与中美利差。**两腿都有值才算**，缺一即跳过（不猜、不填 0）
        cn10 = pd.to_numeric(df.get("中国国债收益率10年"), errors="coerce")
        cn2 = pd.to_numeric(df.get("中国国债收益率2年"), errors="coerce")
        us10 = pd.to_numeric(df.get("美国国债收益率10年"), errors="coerce")
        pts += self._spread(df["_d"], cn10, cn2, "cn_term_spread_10y2y")
        pts += self._spread(df["_d"], cn10, us10, "cn_us_spread_10y")
        return pts

    def _spread(self, dates, a: pd.Series | None, b: pd.Series | None, code: str) -> list[Point]:
        if a is None or b is None:
            return []
        diff = a - b
        return [Point(code=code, as_of=d, value=float(v), source=self.name)
                for d, v in zip(dates, diff) if pd.notna(v)]


class RepoRateAdapter:
    """FDR007 存款类机构7天回购**定盘**利率（`ak.repo_rate_hist`）。

    ⚠️口径：FDR007 是 11:00 定盘利率，**不是 DR007 加权平均利率**——已把口径写进 code。
    实测 `repo_rate_query` 的四个 symbol（回购定盘利率/银行间回购利率/质押式回购/
    存款类机构质押式回购）全部只返回 FR/FDR 系列，akshare 内**确无**加权平均的 DR007。

    ⚠️区间上限：1/3/7/12 个月可用，19 个月即报 `KeyError: frValueMap`；
    逐月排查 2025 全年无坏月份 → 是区间长度限制而非数据缺陷。故按 6 个月切片（留一倍余量）。
    """

    name = "akshare:repo_rate_hist"
    codes = ("fdr007",)
    _SLICE_MONTHS = 6

    def fetch(self, start: str, end: str) -> list[Point]:
        import akshare as ak
        df = sliced_fetch(lambda s, e: ak.repo_rate_hist(start_date=s, end_date=e),
                          start, end, self._SLICE_MONTHS)
        if df.empty or "FDR007" not in df.columns:
            return []
        df = df.copy()
        df["_d"] = df["date"].map(to_ymd)
        df = df[(df["_d"] >= start) & (df["_d"] <= end)]
        return points_from_frame(df, "_d", {"FDR007": "fdr007"}, self.name)
