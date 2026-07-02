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
from app.data.cache import cached_daily, invalidate, rate_limited_call, with_retry
from app.data.provider import DataProvider

logger = logging.getLogger(__name__)

# 本进程已"自愈重取"过的单股序列 key，避免对停牌股(永远无新bar)反复重取
_FRESHENED: set[str] = set()

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

    def get_stock_company(self, ts_code: str) -> pd.DataFrame:
        """公司基本信息（主营业务/经营范围/简介/员工数）。变动极少→缓存一天足够。"""
        import datetime
        key = f"{ts_code}_{datetime.date.today().strftime('%Y%m%d')}"
        return cached_daily(
            name="tushare_stock_company", date_key=key,
            fetch_fn=lambda: self._fetch_stock_company(ts_code),
        )

    @_RETRY
    def _fetch_stock_company(self, ts_code: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_stock_company", self._api.stock_company, ts_code=ts_code,
            fields="ts_code,com_name,main_business,business_scope,introduction,employees,province,city",
        )

    def get_main_business(self, ts_code: str) -> pd.DataFrame:
        """主营业务构成（按产品·营收/利润；用于看主要产品与占比）。缓存一天。"""
        import datetime
        key = f"{ts_code}_{datetime.date.today().strftime('%Y%m%d')}"
        return cached_daily(
            name="tushare_fina_mainbz", date_key=key,
            fetch_fn=lambda: self._fetch_main_business(ts_code),
        )

    @_RETRY
    def _fetch_main_business(self, ts_code: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_fina_mainbz", self._api.fina_mainbz, ts_code=ts_code, type="P",
            fields="ts_code,end_date,bz_item,bz_sales,bz_profit",
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

    def get_kpl_list(self, trade_date: str) -> pd.DataFrame:
        """开盘啦打板榜单（涨停/连板个股 + 题材/封板时间）。按日缓存。"""
        return cached_daily("tushare_kpl_list", trade_date,
                            lambda: self._fetch_kpl_list(trade_date))

    @_RETRY
    def _fetch_kpl_list(self, trade_date: str) -> pd.DataFrame:
        return rate_limited_call("tushare_kpl_list", self._api.kpl_list, trade_date=trade_date)

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

    def get_margin_detail(self, ts_code: str) -> pd.DataFrame:
        """单股融资融券明细（近25天，rzye=融资余额）。缓存一天。"""
        return cached_daily("tushare_margin_detail", self._today_key(ts_code),
                            lambda: self._fetch_margin_detail(ts_code))

    @_RETRY
    def _fetch_margin_detail(self, ts_code: str) -> pd.DataFrame:
        import datetime
        t = datetime.date.today()
        return rate_limited_call(
            "tushare_margin_detail", self._api.margin_detail, ts_code=ts_code,
            start_date=(t - datetime.timedelta(days=25)).strftime("%Y%m%d"),
            end_date=t.strftime("%Y%m%d"))

    def get_repurchase(self, ts_code: str) -> pd.DataFrame:
        """单股股份回购（proc=进度: 完成/实施中/预案）。缓存一天。"""
        return cached_daily("tushare_repurchase", self._today_key(ts_code),
                            lambda: self._fetch_repurchase(ts_code))

    @_RETRY
    def _fetch_repurchase(self, ts_code: str) -> pd.DataFrame:
        return rate_limited_call("tushare_repurchase", self._api.repurchase, ts_code=ts_code)

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

    def get_cyq_perf_by_date(self, trade_date: str) -> pd.DataFrame:
        """**全市场**某交易日筹码分布（含 winner_rate 获利盘）。一次取全市场，缓存一天。"""
        return cached_daily("tushare_cyq_perf_date", trade_date,
                            lambda: rate_limited_call("tushare_cyq_perf_date",
                                                      self._api.cyq_perf, trade_date=trade_date))

    def get_block_trade_by_date(self, trade_date: str) -> pd.DataFrame:
        """**全市场**某交易日大宗交易（成交价/金额/买卖席位）。一次取全市场，缓存一天。"""
        return cached_daily("tushare_block_trade_date", trade_date,
                            lambda: rate_limited_call("tushare_block_trade_date",
                                                      self._api.block_trade, trade_date=trade_date))

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

    def get_fina_indicator_by_period(self, period: str) -> pd.DataFrame:
        """全市场某报告期财务指标（一次取全·供选股批量排雷）。列：ts_code/debt_to_assets/
        netprofit_yoy/roe/or_yoy。按 period 缓存一天。"""
        return cached_daily(
            name="tushare_fina_by_period",
            date_key=period,
            fetch_fn=lambda: self._fetch_fina_by_period(period),
        )

    @_RETRY
    def _fetch_fina_by_period(self, period: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_fina_indicator",
            self._api.fina_indicator_vip,
            period=period,
            fields="ts_code,debt_to_assets,netprofit_yoy,roe,or_yoy",
        )

    def get_forecast_by_period(self, period: str) -> pd.DataFrame:
        """全市场某报告期业绩预告（一次取全·供选股业绩催化）。列：ts_code/type/
        p_change_min/p_change_max。按 period 缓存一天。"""
        return cached_daily(
            name="tushare_forecast_by_period",
            date_key=period,
            fetch_fn=lambda: self._fetch_forecast_by_period(period),
        )

    @_RETRY
    def _fetch_forecast_by_period(self, period: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_forecast",
            self._api.forecast_vip,
            period=period,
            fields="ts_code,ann_date,type,p_change_min,p_change_max",
        )

    def _expected_latest_td(self) -> str:
        """日历上"应已有数据的最近交易日"（≤今天的最近开市日）。失败回退今天。"""
        import datetime
        now = datetime.datetime.now()
        today = now.strftime("%Y%m%d")
        try:
            start = (now - datetime.timedelta(days=15)).strftime("%Y%m%d")
            cal = self.get_trade_cal(start, today)
            days = cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist()
            return max(days) if days else today
        except Exception:
            return today

    def _cached_series_fresh(self, name: str, key: str, end_date: str, fetch_fn) -> pd.DataFrame:
        """带"新鲜度自愈"的单股序列缓存：若缓存停在应有最新交易日之前
        （盘中/早缓存了不完整数据），重取一次覆盖。每 key 每进程最多自愈一次（停牌股不空转）。"""
        df = cached_daily(name=name, date_key=key, fetch_fn=fetch_fn)
        if (df is not None and not df.empty and "trade_date" in df.columns
                and key not in _FRESHENED):
            target = min(str(end_date), self._expected_latest_td())
            if str(df["trade_date"].astype(str).max()) < target:
                _FRESHENED.add(key)
                invalidate(name, key)
                df = cached_daily(name=name, date_key=key, fetch_fn=fetch_fn)
        return df

    def get_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """单股区间日线（按 ts_code 一次拉全，用于个股回测/形态）。列同 daily。"""
        key = f"{ts_code}_{start_date}_{end_date}"
        return self._cached_series_fresh(
            "tushare_stock_daily", key, end_date,
            lambda: self._fetch_stock_daily(ts_code, start_date, end_date),
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
        return self._cached_series_fresh(
            "tushare_stock_adj", key, end_date,
            lambda: self._fetch_stock_adj(ts_code, start_date, end_date),
        )

    @_RETRY
    def _fetch_stock_adj(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_stock_adj",
            self._api.adj_factor,
            ts_code=ts_code, start_date=start_date, end_date=end_date,
        )

    def get_stock_basic(self) -> pd.DataFrame:
        """
        股票基础信息列表（缓存一天）。

        ⚠️ `industry` 列已**覆盖为申万二级**（134个·够细够标准：半导体/消费电子/元件/光学…
        分开），原 Tushare 行业保留在 `industry_src`，申万一级在 `industry_l1`（供上卷）。
        申万映射不可用时优雅回退原 Tushare 行业，保证不劣化。所有按 `industry` 聚合的
        板块分析（行业资金/宽表/全景看板/广度雷达/同类回测）因此统一升级为申万二级口径。
        """
        import datetime
        today = datetime.date.today().strftime("%Y%m%d")
        raw = cached_daily(
            name="tushare_stock_basic",
            date_key=today,
            fetch_fn=self._fetch_stock_basic,
        )
        try:
            return self._overlay_sw_industry(raw)
        except Exception as e:
            logger.warning("[申万] 行业覆盖失败，回退 Tushare 行业: %s", e)
            return raw

    @_RETRY
    def _fetch_stock_basic(self) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_stock_basic",
            self._api.stock_basic,
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,area,industry,market,list_date,circ_mv",
        )

    def _overlay_sw_industry(self, basic: pd.DataFrame) -> pd.DataFrame:
        """
        把 stock_basic.industry 覆盖为**申万二级**（主分析口径：半导体/消费电子/元件… 够细，
        且每板块股票数够多·资金/广度统计稳）；保留原 Tushare 值(industry_src)与申万一级
        (industry_l1·供上卷聚合)。缺映射则回退原 Tushare 行业。
        """
        if basic is None or basic.empty or "industry" not in basic.columns:
            return basic
        sw = self.get_sw_industry_map()
        if sw is None or sw.empty:
            return basic
        out = basic.copy()
        out["industry_src"] = out["industry"]                 # 留底 Tushare 原行业
        l1 = dict(zip(sw["ts_code"], sw["l1_name"]))
        l2 = dict(zip(sw["ts_code"], sw["l2_name"]))
        out["industry_l1"] = out["ts_code"].map(l1)           # 申万一级（供上卷/大方向聚合）
        out["industry"] = out["ts_code"].map(l2).fillna(out["industry_src"])  # 申万二级=主口径，缺则回退
        if "l3_name" in sw.columns:                           # 申万三级（PCB/光纤光缆/封测… 炒股精确口径·广度雷达用）
            out["industry_l3"] = out["ts_code"].map(dict(zip(sw["ts_code"], sw["l3_name"])))
        return out

    def get_sw_industry_map(self) -> pd.DataFrame:
        """
        全市场个股 → 申万行业映射（当前成分 is_new=Y），按 ISO 周缓存（成分变动慢）。

        列：ts_code / l1_code / l1_name / l2_code / l2_name / l3_code / l3_name。
        申万（SW2021）是 A 股机构标准行业口径，比 stock_basic.industry 更规范、更细。
        """
        import datetime
        iso = datetime.date.today().isocalendar()
        return cached_daily(
            name="tushare_sw_industry_l3",                     # _l3: 含申万三级·与旧缓存区分(强制刷新)
            date_key=f"{iso[0]}W{iso[1]:02d}",
            fetch_fn=self._fetch_sw_industry_map,
        )

    @_RETRY
    def _fetch_sw_industry_map(self) -> pd.DataFrame:
        """分页拉取申万当前成分（index_member_all 单页上限 3000，offset 翻页）。含申万三级。"""
        frames: list[pd.DataFrame] = []
        offset = 0
        while True:
            df = rate_limited_call(
                "tushare_sw_member", self._api.index_member_all, is_new="Y",
                fields="l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name",
                offset=offset, limit=3000,
            )
            if df is None or df.empty:
                break
            frames.append(df)
            if len(df) < 3000:        # 末页
                break
            offset += len(df)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True).drop_duplicates("ts_code")
        cols = [c for c in ["ts_code", "l1_code", "l1_name", "l2_code", "l2_name",
                            "l3_code", "l3_name"] if c in out.columns]
        return out[cols]

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
        """指数日线（近90日）。

        缓存自愈：Tushare 指数日线常比个股晚发布，早前拉到「只到昨日」的非空数据若被冻结，
        会让指数曲线永远停在昨天。故仅当数据**已覆盖到请求日**才写缓存；否则返回但不缓存，
        下次自动重取（修复"指数 6.30 后不更新"）。
        """
        import datetime
        from app.data.cache import _cache_path
        end_dt = datetime.datetime.strptime(trade_date, "%Y%m%d")
        start_date = (end_dt - datetime.timedelta(days=90)).strftime("%Y%m%d")
        path = _cache_path(f"tushare_index_{ts_code}", trade_date)

        def _covers(df) -> bool:
            return (df is not None and not df.empty
                    and str(df["trade_date"].astype(str).max()) >= trade_date)

        if path.exists():
            cached = pd.read_parquet(path)
            if _covers(cached):
                return cached
        df = self._fetch_index_daily(ts_code, start_date, trade_date)
        if _covers(df):
            df.to_parquet(path, index=False)
        return df

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

    def get_suspend(self, start_date: str, end_date: str) -> pd.DataFrame:
        """停复牌信息（suspend_d·5100积分可用·suspend_type S=停牌/R=复牌）。按 end_date 缓存。"""
        return cached_daily(
            name="tushare_suspend",
            date_key=end_date,
            fetch_fn=lambda: self._fetch_suspend(start_date, end_date),
        )

    @_RETRY
    def _fetch_suspend(self, start_date: str, end_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_suspend", self._api.suspend_d,
            start_date=start_date, end_date=end_date,
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

    def get_lhb_inst(self, trade_date: str) -> pd.DataFrame:
        """龙虎榜机构席位明细（top_inst，5100积分实测可用）。

        含 `exalter`（席位名，'机构专用'=机构席位）/`buy`/`sell`/`net_buy`（元）。
        是个股级"真机构钱"印证源——上榜时才有，稀疏但高信号。
        """
        return cached_daily(
            name="tushare_lhb_inst",
            date_key=trade_date,
            fetch_fn=lambda: self._fetch_lhb_inst(trade_date),
        )

    @_RETRY
    def _fetch_lhb_inst(self, trade_date: str) -> pd.DataFrame:
        return rate_limited_call(
            "tushare_lhb_inst",
            self._api.top_inst,
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
