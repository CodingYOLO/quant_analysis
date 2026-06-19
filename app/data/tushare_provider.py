"""
Tushare 数据提供者实现（5100积分账号可用接口）。
覆盖：日线行情、股票基础信息、交易日历、指数日线、资金流、龙虎榜、北向资金。
不覆盖的接口由 AkshareProvider 实现，上层通过 CompositeProvider 统一访问。
"""

import logging
from functools import lru_cache

import pandas as pd
import tushare as ts

from app.config import get_settings
from app.data.cache import cached_daily, rate_limited_call, with_retry
from app.data.provider import DataProvider

logger = logging.getLogger(__name__)

_RETRY = with_retry(stop_attempts=3, wait_min=2.0, wait_max=30.0)


@lru_cache(maxsize=1)
def _get_pro_api() -> ts.pro_api:
    """初始化并缓存 Tushare Pro API 实例。"""
    settings = get_settings()
    ts.set_token(settings.tushare_token)
    return ts.pro_api()


class TushareProvider(DataProvider):
    """基于 Tushare Pro 的数据提供者（适用于5100积分账号）。"""

    def __init__(self) -> None:
        self._api = _get_pro_api()

    # ---- 基础行情 ----

    def get_daily(self, trade_date: str) -> pd.DataFrame:
        """全市场日线行情，含 ts_code/open/high/low/close/vol/amount/pct_chg 等。"""
        return cached_daily(
            name="tushare_daily",
            date_key=trade_date,
            fetch_fn=lambda: self._fetch_daily(trade_date),
        )

    @_RETRY
    def _fetch_daily(self, trade_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_daily",
            self._api.daily,
            trade_date=trade_date,
        )

    def get_adj_factor(self, trade_date: str) -> pd.DataFrame:
        """全市场复权因子（用于前复权均线广度）。列：ts_code/trade_date/adj_factor。"""
        return cached_daily(
            name="tushare_adj_factor",
            date_key=trade_date,
            fetch_fn=lambda: self._fetch_adj_factor(trade_date),
        )

    @_RETRY
    def _fetch_adj_factor(self, trade_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_adj_factor",
            self._api.adj_factor,
            trade_date=trade_date,
        )

    def get_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """单股区间日线（按 ts_code 一次拉全，用于个股回测/形态）。列同 daily。"""
        key = f"{ts_code}_{start_date}_{end_date}"
        return cached_daily(
            name="tushare_stock_daily",
            date_key=key,
            fetch_fn=lambda: self._fetch_stock_daily(ts_code, start_date, end_date),
        )

    @_RETRY
    def _fetch_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_stock_daily",
            self._api.daily,
            ts_code=ts_code, start_date=start_date, end_date=end_date,
        )

    def get_adj_factor_series(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """单股区间复权因子。列：trade_date/adj_factor。"""
        key = f"{ts_code}_{start_date}_{end_date}"
        return cached_daily(
            name="tushare_stock_adj",
            date_key=key,
            fetch_fn=lambda: self._fetch_stock_adj(ts_code, start_date, end_date),
        )

    @_RETRY
    def _fetch_stock_adj(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_stock_adj",
            self._api.adj_factor,
            ts_code=ts_code, start_date=start_date, end_date=end_date,
        )

    def get_stock_basic(self) -> pd.DataFrame:
        """股票基础信息列表（缓存一天）。"""
        import datetime
        today = datetime.date.today().strftime("%Y%m%d")
        return cached_daily(
            name="tushare_stock_basic",
            date_key=today,
            fetch_fn=self._fetch_stock_basic,
        )

    @_RETRY
    def _fetch_stock_basic(self) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_stock_basic",
            self._api.stock_basic,
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,area,industry,market,list_date,circ_mv",
        )

    def get_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        """交易日历。"""
        key = f"{start_date}_{end_date}"
        return cached_daily(
            name="tushare_trade_cal",
            date_key=key,
            fetch_fn=lambda: self._fetch_trade_cal(start_date, end_date),
        )

    @_RETRY
    def _fetch_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_trade_cal",
            self._api.trade_cal,
            exchange="SSE",
            start_date=start_date,
            end_date=end_date,
        )

    def get_index_daily(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """指数日线（近60日，用于MA计算）。"""
        import datetime
        end_dt = datetime.datetime.strptime(trade_date, "%Y%m%d")
        start_dt = end_dt - datetime.timedelta(days=90)
        start_date = start_dt.strftime("%Y%m%d")

        return cached_daily(
            name=f"tushare_index_{ts_code}",
            date_key=trade_date,
            fetch_fn=lambda: self._fetch_index_daily(ts_code, start_date, trade_date),
        )

    @_RETRY
    def _fetch_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_index_daily",
            self._api.index_daily,
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )

    # ---- 资金与龙虎榜 ----

    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """
        每日基础指标：市值、换手率、量比、PE、PB 等。
        关键字段：ts_code, total_mv, circ_mv, turnover_rate, volume_ratio, pe_ttm, pb
        total_mv / circ_mv 单位：万元，需 /10000 转换为亿元。
        """
        return cached_daily(
            name="tushare_daily_basic",
            date_key=trade_date,
            fetch_fn=lambda: self._fetch_daily_basic(trade_date),
        )

    @_RETRY
    def _fetch_daily_basic(self, trade_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_daily_basic",
            self._api.daily_basic,
            trade_date=trade_date,
            fields="ts_code,trade_date,close,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv",
        )

    def get_money_flow(self, trade_date: str) -> pd.DataFrame:
        """个股资金流（需2000积分，5100积分账号可用）。"""
        return cached_daily(
            name="tushare_moneyflow",
            date_key=trade_date,
            fetch_fn=lambda: self._fetch_money_flow(trade_date),
        )

    @_RETRY
    def _fetch_money_flow(self, trade_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_moneyflow",
            self._api.moneyflow,
            trade_date=trade_date,
        )

    def get_lhb_detail(self, trade_date: str) -> pd.DataFrame:
        """龙虎榜明细（需2000积分，5100积分账号可用）。"""
        return cached_daily(
            name="tushare_lhb",
            date_key=trade_date,
            fetch_fn=lambda: self._fetch_lhb(trade_date),
        )

    @_RETRY
    def _fetch_lhb(self, trade_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_lhb",
            self._api.top_list,
            trade_date=trade_date,
        )

    def get_north_flow(self, trade_date: str) -> pd.DataFrame:
        """北向资金汇总（需2000积分，5100积分账号可用）。"""
        return cached_daily(
            name="tushare_north_flow",
            date_key=trade_date,
            fetch_fn=lambda: self._fetch_north_flow(trade_date),
        )

    @_RETRY
    def _fetch_north_flow(self, trade_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_north_flow",
            self._api.moneyflow_hsgt,
            trade_date=trade_date,
        )

    # ---- 以下接口 Tushare 不覆盖，由 AkshareProvider 实现 ----

    def get_concept_list(self) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_concept_list()")

    def get_concept_members(self, concept_code: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_concept_members()")

    def get_industry_list(self) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_industry_list()")

    def get_industry_members(self, industry_code: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_industry_members()")

    def get_sector_fund_flow(self) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_sector_fund_flow()")

    def get_stock_comment(self, trade_date: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_stock_comment()")

    def get_cls_news(self, date: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_cls_news()")

    def get_stock_news(self, ts_code: str) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_stock_news()")

    def get_spot_em(self) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_spot_em()")

    def get_realtime_quote(self, ts_codes: list[str]) -> pd.DataFrame:
        raise NotImplementedError("请使用 AkshareProvider.get_realtime_quote()")
