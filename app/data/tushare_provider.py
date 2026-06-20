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

    def get_forecast(self, ts_code: str) -> pd.DataFrame:
        """单股业绩预告（预增/预减/扭亏/首亏等 + 净利变动幅度）。缓存一天。"""
        import datetime
        key = f"{ts_code}_{datetime.date.today().strftime('%Y%m%d')}"
        return cached_daily(
            name="tushare_forecast",
            date_key=key,
            fetch_fn=lambda: self._fetch_forecast(ts_code),
        )

    @_RETRY
    def _fetch_forecast(self, ts_code: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_forecast",
            self._api.forecast,
            ts_code=ts_code,
            fields="ts_code,ann_date,end_date,type,p_change_min,p_change_max,summary,change_reason",
        )

    def get_survey(self, ts_code: str) -> pd.DataFrame:
        """单股机构调研记录（近一年；调研热度=关注度信号）。缓存一天。"""
        import datetime
        key = f"{ts_code}_{datetime.date.today().strftime('%Y%m%d')}"
        return cached_daily(
            name="tushare_stk_surv",
            date_key=key,
            fetch_fn=lambda: self._fetch_survey(ts_code),
        )

    @_RETRY
    def _fetch_survey(self, ts_code: str) -> pd.DataFrame:
        import datetime
        start = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y%m%d")
        end = datetime.date.today().strftime("%Y%m%d")
        return rate_limited_call(
            "tushare_stk_surv", self._api.stk_surv,
            ts_code=ts_code, start_date=start, end_date=end,
        )

    def get_report_rc(self, ts_code: str) -> pd.DataFrame:
        """单股券商盈利预测/目标价（report_rc）。⚠️5100档限频1次/小时 → 日缓存兜底。"""
        import datetime
        key = f"{ts_code}_{datetime.date.today().strftime('%Y%m%d')}"
        return cached_daily(
            name="tushare_report_rc",
            date_key=key,
            fetch_fn=lambda: self._fetch_report_rc(ts_code),
        )

    def _fetch_report_rc(self, ts_code: str) -> pd.DataFrame:
        # 不重试：限频(1次/小时)重试无意义，失败快速返回由上层优雅降级
        import datetime
        start = (datetime.date.today() - datetime.timedelta(days=180)).strftime("%Y%m%d")
        end = datetime.date.today().strftime("%Y%m%d")
        return rate_limited_call(
            "tushare_report_rc", self._api.report_rc,
            ts_code=ts_code, start_date=start, end_date=end,
        )

    def get_limit_list(self, trade_date: str, limit_type: str = "U") -> pd.DataFrame:
        """官方涨跌停/炸板榜（limit_list_d）。limit_type: U涨停/D跌停/Z炸板。按(日期+类型)缓存。"""
        return cached_daily(
            name=f"tushare_limit_{limit_type}",
            date_key=trade_date,
            fetch_fn=lambda: self._fetch_limit_list(trade_date, limit_type),
        )

    @_RETRY
    def _fetch_limit_list(self, trade_date: str, limit_type: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_limit_list", self._api.limit_list_d,
            trade_date=trade_date, limit_type=limit_type,
        )

    # ---- 事件/避雷面（解禁/增减持/快报/户数）----

    def _today_key(self, ts_code: str) -> str:
        import datetime
        return f"{ts_code}_{datetime.date.today().strftime('%Y%m%d')}"

    def get_share_float(self, ts_code: str) -> pd.DataFrame:
        """单股限售解禁（近30天~未来约1年，float_date=解禁日）。缓存一天。"""
        return cached_daily("tushare_share_float", self._today_key(ts_code),
                            lambda: self._fetch_share_float(ts_code))

    @_RETRY
    def _fetch_share_float(self, ts_code: str) -> pd.DataFrame:
        import datetime
        t = datetime.date.today()
        return rate_limited_call(
            "tushare_share_float", self._api.share_float, ts_code=ts_code,
            start_date=(t - datetime.timedelta(days=30)).strftime("%Y%m%d"),
            end_date=(t + datetime.timedelta(days=400)).strftime("%Y%m%d"))

    def get_holder_trade(self, ts_code: str) -> pd.DataFrame:
        """单股股东增减持（近180天，in_de=IN增持/DE减持）。缓存一天。"""
        return cached_daily("tushare_holdertrade", self._today_key(ts_code),
                            lambda: self._fetch_holder_trade(ts_code))

    @_RETRY
    def _fetch_holder_trade(self, ts_code: str) -> pd.DataFrame:
        import datetime
        t = datetime.date.today()
        return rate_limited_call(
            "tushare_holdertrade", self._api.stk_holdertrade, ts_code=ts_code,
            start_date=(t - datetime.timedelta(days=180)).strftime("%Y%m%d"),
            end_date=t.strftime("%Y%m%d"))

    def get_express(self, ts_code: str) -> pd.DataFrame:
        """单股业绩快报（近400天，比业绩预告更接近真实）。缓存一天。"""
        return cached_daily("tushare_express", self._today_key(ts_code),
                            lambda: self._fetch_express(ts_code))

    @_RETRY
    def _fetch_express(self, ts_code: str) -> pd.DataFrame:
        import datetime
        t = datetime.date.today()
        return rate_limited_call(
            "tushare_express", self._api.express, ts_code=ts_code,
            start_date=(t - datetime.timedelta(days=400)).strftime("%Y%m%d"),
            end_date=t.strftime("%Y%m%d"))

    def get_holder_number(self, ts_code: str) -> pd.DataFrame:
        """单股股东户数（近400天，户数减少=筹码集中）。缓存一天。"""
        return cached_daily("tushare_holdernum", self._today_key(ts_code),
                            lambda: self._fetch_holder_number(ts_code))

    @_RETRY
    def _fetch_holder_number(self, ts_code: str) -> pd.DataFrame:
        import datetime
        t = datetime.date.today()
        return rate_limited_call(
            "tushare_holdernum", self._api.stk_holdernumber, ts_code=ts_code,
            start_date=(t - datetime.timedelta(days=400)).strftime("%Y%m%d"),
            end_date=t.strftime("%Y%m%d"))

    def get_block_trade(self, ts_code: str) -> pd.DataFrame:
        """单股大宗交易（近180天，含成交价/金额/买卖席位）。缓存一天。"""
        return cached_daily("tushare_block_trade", self._today_key(ts_code),
                            lambda: self._fetch_block_trade(ts_code))

    @_RETRY
    def _fetch_block_trade(self, ts_code: str) -> pd.DataFrame:
        import datetime
        t = datetime.date.today()
        return rate_limited_call(
            "tushare_block_trade", self._api.block_trade, ts_code=ts_code,
            start_date=(t - datetime.timedelta(days=180)).strftime("%Y%m%d"),
            end_date=t.strftime("%Y%m%d"))

    def get_cyq_perf(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """单股筹码分布（每日：加权平均成本/获利盘比例/各分位成本）。缓存一天。"""
        import datetime
        key = f"{ts_code}_{start_date}_{end_date}_{datetime.date.today().strftime('%Y%m%d')}"
        return cached_daily(
            name="tushare_cyq_perf",
            date_key=key,
            fetch_fn=lambda: self._fetch_cyq_perf(ts_code, start_date, end_date),
        )

    @_RETRY
    def _fetch_cyq_perf(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_cyq_perf",
            self._api.cyq_perf,
            ts_code=ts_code, start_date=start_date, end_date=end_date,
        )

    def get_fina_indicator(self, ts_code: str) -> pd.DataFrame:
        """单股财务指标（ROE/营收净利同比/负债率/毛利率等，多期）。缓存一天。"""
        import datetime
        key = f"{ts_code}_{datetime.date.today().strftime('%Y%m%d')}"
        return cached_daily(
            name="tushare_fina_indicator",
            date_key=key,
            fetch_fn=lambda: self._fetch_fina_indicator(ts_code),
        )

    @_RETRY
    def _fetch_fina_indicator(self, ts_code: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_fina_indicator",
            self._api.fina_indicator,
            ts_code=ts_code,
            fields="ts_code,end_date,roe,roe_dt,netprofit_yoy,or_yoy,debt_to_assets,grossprofit_margin",
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

    def get_index_daily_range(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """指数区间日线（[start, end] 全段，按 ts_code+区间缓存；用于回测大盘状态分层）。"""
        key = f"{ts_code}_{start_date}_{end_date}"
        return cached_daily(
            name="tushare_index_range",
            date_key=key,
            fetch_fn=lambda: self._fetch_index_daily(ts_code, start_date, end_date),
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
