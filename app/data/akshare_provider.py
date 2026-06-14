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
        """
        财联社电报/快讯（当日实时）+ 东方财富财经要闻（支持历史日期）。
        优先返回缓存；若当日缓存不存在则实时拉取财联社；
        同时拉取东方财富历史要闻作为补充。
        """
        return cached_daily(
            name="ak_cls_news",
            date_key=date,
            fetch_fn=self._fetch_cls_news,
        )

    def _fetch_cls_news(self) -> pd.DataFrame:
        """
        优先用财联社电报（/api/cache?name=telegraph，需 CLS_COOKIE），
        降级到东方财富财经快讯。两路合并去重，最多返回 80 条。
        """
        from app.config import get_settings
        frames = []

        # 1. 财联社电报（从 JS bundle 逆向出的真实端点，需 Cookie）
        cls_cookie = get_settings().cls_cookie
        if cls_cookie:
            df_cls = self._fetch_cls_with_cookie(cls_cookie)
            if df_cls is not None and not df_cls.empty:
                df_cls["来源"] = "财联社"
                frames.append(df_cls)
                logger.info("财联社电报: 获取 %d 条", len(df_cls))

        # 2. 东方财富财经快讯（备用/补充）
        try:
            df_em = rate_limited_call("ak_em_global_news", ak.stock_info_global_em)
            if df_em is not None and not df_em.empty:
                df_em["来源"] = "东方财富"
                frames.append(df_em)
        except Exception as e:
            logger.debug("东方财富财经快讯获取失败: %s", e)

        if not frames:
            return pd.DataFrame()

        # 统一 发布时间 为字符串，避免 parquet 序列化时类型冲突
        for df in frames:
            if "发布时间" in df.columns:
                df["发布时间"] = df["发布时间"].astype(str)

        combined = pd.concat(frames, ignore_index=True)
        if "标题" in combined.columns:
            combined = combined.drop_duplicates(subset=["标题"], keep="first")
        return combined.head(80)

    def _fetch_cls_with_cookie(self, cookie: str) -> pd.DataFrame | None:
        """
        财联社电报真实 API：/api/cache?name=telegraph（从 telegraph JS bundle 逆向）。
        不需要 sign 参数，只需登录 Cookie。
        Cookie 过期/失效时静默降级，不抛异常。
        """
        import requests
        import time

        url = "https://www.cls.cn/api/cache"
        params = {
            "rn": 60,
            "lastTime": int(time.time()),
            "name": "telegraph",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.cls.cn/telegraph",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=8)
            if resp.status_code != 200:
                logger.debug("财联社HTTP%d", resp.status_code)
                return None

            data = resp.json()
            if data.get("errno", -1) != 0:
                logger.debug("财联社返回错误errno=%s: %s", data.get("errno"), data.get("msg"))
                return None

            items = data.get("data", {}).get("roll_data", [])
            if not items:
                return None

            rows = []
            for item in items:
                title = str(item.get("title", "")).strip()
                brief = str(item.get("brief", item.get("content", ""))).strip()
                text = f"{title} {brief}".strip() if brief and brief != title else title
                ctime = item.get("ctime", 0)
                try:
                    ts = pd.to_datetime(ctime, unit="s", utc=True).tz_convert("Asia/Shanghai")
                except Exception:
                    ts = pd.NaT
                rows.append({"标题": text, "发布时间": ts, "等级": item.get("level", "")})

            return pd.DataFrame(rows)

        except Exception as e:
            logger.debug("财联社Cookie请求失败: %s", e)
            return None

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
