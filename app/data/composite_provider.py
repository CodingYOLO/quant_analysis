"""
CompositeProvider: 组合 Tushare + Akshare，对外暴露统一的 DataProvider 接口。
上层模块只需注入 CompositeProvider，无需感知底层数据源。
"""

import pandas as pd

from app.data.provider import DataProvider
from app.data.tushare_provider import TushareProvider
from app.data.akshare_provider import AkshareProvider


class CompositeProvider(DataProvider):
    """统一数据入口：Tushare 负责行情/资金/龙虎榜，Akshare 负责其余。"""

    def __init__(
        self,
        tushare: TushareProvider | None = None,
        akshare: AkshareProvider | None = None,
    ) -> None:
        self._ts = tushare or TushareProvider()
        self._ak = akshare or AkshareProvider()

    # ---- Tushare 接口 ----

    def get_daily(self, trade_date: str) -> pd.DataFrame:
        return self._ts.get_daily(trade_date)

    def get_stock_basic(self) -> pd.DataFrame:
        return self._ts.get_stock_basic()

    def get_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self._ts.get_trade_cal(start_date, end_date)

    def get_index_daily(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        return self._ts.get_index_daily(ts_code, trade_date)

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        return self._ts.get_daily_basic(trade_date)

    def get_money_flow(self, trade_date: str) -> pd.DataFrame:
        return self._ts.get_money_flow(trade_date)

    def get_lhb_detail(self, trade_date: str) -> pd.DataFrame:
        return self._ts.get_lhb_detail(trade_date)

    def get_north_flow(self, trade_date: str) -> pd.DataFrame:
        return self._ts.get_north_flow(trade_date)

    # ---- Akshare 接口 ----

    def get_spot_em(self) -> pd.DataFrame:
        return self._ak.get_spot_em()

    def get_realtime_quote(self, ts_codes: list[str]) -> pd.DataFrame:
        return self._ak.get_realtime_quote(ts_codes)

    def get_concept_list(self) -> pd.DataFrame:
        return self._ak.get_concept_list()

    def get_concept_members(self, concept_code: str) -> pd.DataFrame:
        return self._ak.get_concept_members(concept_code)

    def get_industry_list(self) -> pd.DataFrame:
        return self._ak.get_industry_list()

    def get_industry_members(self, industry_code: str) -> pd.DataFrame:
        return self._ak.get_industry_members(industry_code)

    def get_sector_fund_flow(self) -> pd.DataFrame:
        return self._ak.get_sector_fund_flow()

    def get_stock_comment(self, trade_date: str) -> pd.DataFrame:
        return self._ak.get_stock_comment(trade_date)

    def get_cls_news(self, date: str) -> pd.DataFrame:
        return self._ak.get_cls_news(date)

    def get_stock_news(self, ts_code: str) -> pd.DataFrame:
        return self._ak.get_stock_news(ts_code)

    def check_cls_health(self) -> dict:
        """财联社 Cookie 健康检查（委托 Akshare 实现）。"""
        return self._ak.check_cls_health()
