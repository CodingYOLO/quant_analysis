"""
Akshare 数据提供者实现（免费接口）。
覆盖 Tushare 不提供的数据：实时快照、板块概念、千股千评、财联社新闻等。
注意：akshare 接口签名会变化，所有字段名以 verify.py 真实验证结果为准。
"""

import logging
import datetime

import pandas as pd
import akshare as ak

from app.data.cache import cached_daily, rate_limited_call, with_retry
from app.data.provider import DataProvider

logger = logging.getLogger(__name__)

_RETRY = with_retry(stop_attempts=3, wait_min=2.0, wait_max=30.0)


def _disable_proxy_for_akshare() -> None:
    """
    清除代理环境变量，让 akshare（requests库）直连国内数据源。
    本地挂 VPN 时必须调用，否则东方财富等接口会被代理拦截。
    部署到国内服务器时无副作用（服务器通常没有代理环境变量）。
    """
    import os
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(key, None)


_disable_proxy_for_akshare()


class AkshareProvider(DataProvider):
    """基于 akshare 的数据提供者（免费，覆盖 Tushare 不足的部分）。"""

    # ---- 实时快照 ----

    def get_spot_em(self) -> pd.DataFrame:
        """全市场收盘快照（东方财富），含价格、涨跌幅、成交额、市值等。"""
        today = datetime.date.today().strftime("%Y%m%d")
        return cached_daily(
            name="ak_spot_em",
            date_key=today,
            fetch_fn=self._fetch_spot_em,
        )

    @_RETRY
    def _fetch_spot_em(self) -> pd.DataFrame:
        return rate_limited_call("ak_spot_em", ak.stock_zh_a_spot_em)

    # ---- 板块与概念 ----

    def get_concept_list(self) -> pd.DataFrame:
        """概念板块列表。"""
        today = datetime.date.today().strftime("%Y%m%d")
        return cached_daily(
            name="ak_concept_list",
            date_key=today,
            fetch_fn=self._fetch_concept_list,
        )

    @_RETRY
    def _fetch_concept_list(self) -> pd.DataFrame:
        return rate_limited_call("ak_concept_list", ak.stock_board_concept_name_em)

    def get_concept_members(self, concept_code: str) -> pd.DataFrame:
        """概念板块成分股（不缓存，按需拉取）。"""
        return rate_limited_call(
            "ak_concept_members",
            ak.stock_board_concept_cons_em,
            symbol=concept_code,
        )

    def get_industry_list(self) -> pd.DataFrame:
        """行业板块列表。"""
        today = datetime.date.today().strftime("%Y%m%d")
        return cached_daily(
            name="ak_industry_list",
            date_key=today,
            fetch_fn=self._fetch_industry_list,
        )

    @_RETRY
    def _fetch_industry_list(self) -> pd.DataFrame:
        return rate_limited_call("ak_industry_list", ak.stock_board_industry_name_em)

    def get_industry_members(self, industry_code: str) -> pd.DataFrame:
        """行业板块成分股。"""
        return rate_limited_call(
            "ak_industry_members",
            ak.stock_board_industry_cons_em,
            symbol=industry_code,
        )

    def get_sector_fund_flow(self) -> pd.DataFrame:
        """板块资金流排名（按今日涨跌幅排序）。"""
        today = datetime.date.today().strftime("%Y%m%d")
        return cached_daily(
            name="ak_sector_fund_flow",
            date_key=today,
            fetch_fn=self._fetch_sector_fund_flow,
        )

    @_RETRY
    def _fetch_sector_fund_flow(self) -> pd.DataFrame:
        return rate_limited_call(
            "ak_sector_fund_flow",
            ak.stock_sector_fund_flow_rank,
            indicator="今日",
            sector_type="行业资金流",
        )

    # ---- 情绪与舆情 ----

    def get_stock_comment(self, trade_date: str) -> pd.DataFrame:
        """千股千评（综合情绪评分）。"""
        return cached_daily(
            name="ak_stock_comment",
            date_key=trade_date,
            fetch_fn=self._fetch_stock_comment,
        )

    @_RETRY
    def _fetch_stock_comment(self) -> pd.DataFrame:
        return rate_limited_call("ak_stock_comment", ak.stock_comment_em)

    def get_cls_news(self, date: str) -> pd.DataFrame:
        """财联社电报/快讯。"""
        return cached_daily(
            name="ak_cls_news",
            date_key=date,
            fetch_fn=self._fetch_cls_news,
        )

    @_RETRY
    def _fetch_cls_news(self) -> pd.DataFrame:
        return rate_limited_call("ak_cls_news", ak.stock_info_global_cls)

    def get_stock_news(self, ts_code: str) -> pd.DataFrame:
        """个股新闻（不缓存，按需拉取）。"""
        # akshare 使用6位代码，去掉后缀
        symbol = ts_code.split(".")[0]
        return rate_limited_call(
            "ak_stock_news",
            ak.stock_news_em,
            symbol=symbol,
        )

    # ---- 以下接口由 TushareProvider 实现 ----

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 TushareProvider.get_daily_basic()")

    def get_daily(self, trade_date: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 TushareProvider.get_daily()")

    def get_stock_basic(self) -> pd.DataFrame:
        raise NotImplementedError("请使用 TushareProvider.get_stock_basic()")

    def get_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 TushareProvider.get_trade_cal()")

    def get_index_daily(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 TushareProvider.get_index_daily()")

    def get_money_flow(self, trade_date: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 TushareProvider.get_money_flow()")

    def get_lhb_detail(self, trade_date: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 TushareProvider.get_lhb_detail()")

    def get_north_flow(self, trade_date: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 TushareProvider.get_north_flow()")
