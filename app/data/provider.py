"""
DataProvider 抽象基类。
上层模块只依赖这个接口，不直接调用 akshare 或 tushare。
"""

from abc import ABC, abstractmethod
import pandas as pd


class DataProvider(ABC):
    """所有数据提供者必须实现的统一接口。"""

    # ---- 基础行情 ----

    @abstractmethod
    def get_daily(self, trade_date: str) -> pd.DataFrame:
        """获取全市场日线行情。返回含 ts_code/open/high/low/close/vol/pct_chg 等列。"""

    @abstractmethod
    def get_stock_basic(self) -> pd.DataFrame:
        """获取全市场股票基础信息列表。"""

    @abstractmethod
    def get_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        """获取交易日历。"""

    @abstractmethod
    def get_index_daily(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """获取指数日线数据（用于大盘状态判断）。"""

    # ---- 资金与龙虎榜 ----

    @abstractmethod
    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """获取每日基础指标（市值、换手率、量比、PE/PB）。"""

    @abstractmethod
    def get_money_flow(self, trade_date: str) -> pd.DataFrame:
        """获取个股资金流数据。"""

    @abstractmethod
    def get_lhb_detail(self, trade_date: str) -> pd.DataFrame:
        """获取龙虎榜明细。"""

    @abstractmethod
    def get_north_flow(self, trade_date: str) -> pd.DataFrame:
        """获取北向资金汇总。"""

    # ---- 板块与概念 ----

    @abstractmethod
    def get_concept_list(self) -> pd.DataFrame:
        """获取概念板块列表。"""

    @abstractmethod
    def get_concept_members(self, concept_code: str) -> pd.DataFrame:
        """获取概念板块成分股。"""

    @abstractmethod
    def get_industry_list(self) -> pd.DataFrame:
        """获取行业板块列表。"""

    @abstractmethod
    def get_industry_members(self, industry_code: str) -> pd.DataFrame:
        """获取行业板块成分股。"""

    @abstractmethod
    def get_sector_fund_flow(self) -> pd.DataFrame:
        """获取板块/概念资金流排名。"""

    # ---- 情绪与舆情 ----

    @abstractmethod
    def get_stock_comment(self, trade_date: str) -> pd.DataFrame:
        """获取千股千评数据。"""

    @abstractmethod
    def get_cls_news(self, date: str) -> pd.DataFrame:
        """获取财联社电报/快讯。"""

    @abstractmethod
    def get_stock_news(self, ts_code: str) -> pd.DataFrame:
        """获取个股新闻。"""

    # ---- 实时快照 ----

    @abstractmethod
    def get_spot_em(self) -> pd.DataFrame:
        """获取全市场实时/收盘快照（东方财富）。"""
