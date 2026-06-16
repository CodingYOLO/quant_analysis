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

    # ---- 实时报价（新浪，指数+个股，轻量按需）----

    def get_realtime_quote(self, ts_codes: list[str]) -> pd.DataFrame:
        """
        新浪实时行情（指数/个股通用），单次批量请求，不缓存。

        选用新浪源（hq.sinajs.cn）而非东方财富全市场扫描：
        新浪只查指定标的、毫秒级、且在国内服务器可直连（东财扫描接口被封）。
        盘后调用返回当日收盘快照，仍可用。

        Args:
            ts_codes: Tushare 格式代码（指数与个股通用），如 ['000001.SH', '600000.SH']

        Returns:
            DataFrame，列 ts_code/name/price/pct_chg/open/high/low/prev_close/amount。
            行序与入参一致；解析失败/无效（现价≤0 且无昨收）的标的被剔除。
        """
        if not ts_codes:
            return pd.DataFrame()

        sina_codes = [self._ts_to_sina(c) for c in ts_codes]
        # 过滤掉无法映射的代码，并保留 sina_code → ts_code 反查
        sina2ts = {s: t for s, t in zip(sina_codes, ts_codes) if s}
        if not sina2ts:
            return pd.DataFrame()

        raw = self._fetch_sina_quote(list(sina2ts.keys()))
        rows = self._parse_sina_quote(raw, sina2ts)
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        # 按入参顺序排序，便于上层稳定展示（指数在前等）
        order = {c: i for i, c in enumerate(ts_codes)}
        df["_ord"] = df["ts_code"].map(order)
        return df.sort_values("_ord").drop(columns="_ord").reset_index(drop=True)

    @staticmethod
    def _ts_to_sina(ts_code: str) -> str:
        """
        Tushare 代码 → 新浪代码。
        '000001.SH'→'sh000001'  '300750.SZ'→'sz300750'  '430047.BJ'→'bj430047'。
        无法识别返回空串。
        """
        try:
            num, mkt = ts_code.split(".")
        except ValueError:
            return ""
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt.upper(), "")
        return f"{prefix}{num}" if prefix else ""

    @_RETRY
    def _fetch_sina_quote(self, sina_codes: list[str]) -> str:
        """请求新浪行情接口，返回 GBK 解码后的原始文本。需带 Referer 否则 403。"""
        import requests

        url = "https://hq.sinajs.cn/list=" + ",".join(sina_codes)
        headers = {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
        }
        resp = rate_limited_call(
            "sina_quote", requests.get, url, headers=headers, timeout=8
        )
        resp.raise_for_status()
        resp.encoding = "gbk"
        return resp.text

    @staticmethod
    def _parse_sina_quote(raw: str, sina2ts: dict[str, str]) -> list[dict]:
        """
        解析新浪行情文本。指数与个股字段排列一致：
          [0]名称 [1]今开 [2]昨收 [3]现价 [4]最高 [5]最低 … [9]成交额。
        """
        rows: list[dict] = []
        for line in raw.strip().split("\n"):
            if 'hq_str_' not in line or '="' not in line:
                continue
            sina_code = line.split("hq_str_", 1)[1].split("=", 1)[0]
            ts_code = sina2ts.get(sina_code)
            if not ts_code:
                continue
            payload = line.split('="', 1)[1].rstrip('";').strip()
            fields = payload.split(",")
            if len(fields) < 6 or not fields[0]:
                continue
            try:
                prev_close = float(fields[2])
                price = float(fields[3])
            except (ValueError, IndexError):
                continue
            # 停牌/未开盘：现价为 0 时回退到昨收，避免显示 0
            if price <= 0:
                price = prev_close
            if prev_close <= 0:
                continue
            pct = (price - prev_close) / prev_close * 100
            rows.append({
                "ts_code": ts_code,
                "name": fields[0],
                "price": round(price, 3),
                "pct_chg": round(pct, 2),
                "open": float(fields[1]) if fields[1] else 0.0,
                "high": float(fields[4]) if fields[4] else 0.0,
                "low": float(fields[5]) if fields[5] else 0.0,
                "prev_close": round(prev_close, 3),
                "amount": float(fields[9]) if len(fields) > 9 and fields[9] else 0.0,
            })
        return rows

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

        Cookie 过期/失效时降级，不抛异常；但与旧实现不同，此处对
        「Cookie 失效」打 WARNING（而非静默 debug），便于及时发现需更新 Cookie。
        """
        result = self._request_cls_roll(cookie)
        status = result["status"]

        if status == "expired":
            logger.warning(
                "财联社 Cookie 已失效（%s），新闻已降级到东方财富。"
                "请在 163 邮箱登录 cls.cn 后更新 .env 的 CLS_COOKIE。",
                result.get("detail", ""),
            )
            return None
        if status != "ok":
            logger.debug("财联社电报不可用（%s）：%s", status, result.get("detail", ""))
            return None

        rows = []
        for item in result["items"]:
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

    def _request_cls_roll(self, cookie: str) -> dict:
        """
        底层请求财联社电报接口，返回结构化结果（供拉取与健康检查共用）。

        Returns:
            dict，含:
              - status: 'ok' | 'expired' | 'empty' | 'network_error'
                  ok           — 鉴权成功且取到数据
                  expired      — Cookie 失效/鉴权失败（HTTP 401/403 或 errno!=0），需更新
                  empty        — 鉴权成功但无数据（接口异常，非 Cookie 问题）
                  network_error— 网络/超时/解析异常
              - items:  list，电报原始条目（status=='ok' 时有值）
              - detail: str，便于日志展示的简短说明
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
            # 401/403 是典型的 Cookie 失效信号
            if resp.status_code in (401, 403):
                return {"status": "expired", "items": [], "detail": f"HTTP{resp.status_code}"}
            if resp.status_code != 200:
                return {"status": "network_error", "items": [], "detail": f"HTTP{resp.status_code}"}

            data = resp.json()
            errno = data.get("errno", -1)
            if errno != 0:
                # errno!=0 多为登录态失效（如未登录、token 过期）
                return {
                    "status": "expired",
                    "items": [],
                    "detail": f"errno={errno} msg={data.get('msg', '')}",
                }

            items = data.get("data", {}).get("roll_data", []) or []
            if not items:
                return {"status": "empty", "items": [], "detail": "roll_data 为空"}

            return {"status": "ok", "items": items, "detail": f"{len(items)}条"}

        except Exception as e:
            return {"status": "network_error", "items": [], "detail": str(e)}

    def check_cls_health(self) -> dict:
        """
        财联社 Cookie 健康检查（供 CLI `cls-check` 调用）。

        主动打一次电报接口，明确返回 Cookie 是否仍然有效，
        用于在 Cookie 到期前（约 2026-08）手动核验。

        Returns:
            dict，含:
              - ok:     bool，Cookie 是否有效
              - status: str，'no_cookie'|'ok'|'expired'|'empty'|'network_error'
              - detail: str，简短说明
              - count:  int，本次取到的电报条数（status=='ok' 时）
        """
        from app.config import get_settings

        cookie = get_settings().cls_cookie
        if not cookie:
            return {"ok": False, "status": "no_cookie", "detail": "未配置 CLS_COOKIE", "count": 0}

        result = self._request_cls_roll(cookie)
        return {
            "ok": result["status"] == "ok",
            "status": result["status"],
            "detail": result.get("detail", ""),
            "count": len(result.get("items", [])),
        }

    def get_wscn_lives(self, date: str) -> pd.DataFrame:
        """
        华尔街见闻全球快讯（global-channel）。

        Args:
            date: YYYYMMDD，用于缓存键；实际拉取最新 ~100 条后按日期过滤。

        Returns:
            DataFrame，含 标题、内容、发布时间、来源 字段，
            其中 发布时间 为带时区的 Timestamp（Asia/Shanghai）。
        """
        return cached_daily(
            name="ak_wscn_lives",
            date_key=date,
            fetch_fn=self._fetch_wscn_lives,
        )

    @_RETRY
    def _fetch_wscn_lives(self) -> pd.DataFrame:
        """调用华尔街见闻非公开 API，翻页拉取最多 100 条当日快讯。"""
        import re
        import time as _time
        import requests

        url = "https://api-one.wallstcn.com/apiv1/content/lives"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Referer": "https://wallstreetcn.com/live/global",
            "Accept": "application/json",
        }

        rows: list[dict] = []
        cursor = ""
        for _ in range(5):  # 最多翻 5 页，每页 20 条
            params: dict = {"channel": "global-channel", "limit": 20}
            if cursor:
                params["cursor"] = cursor

            resp = requests.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data", {}).get("items", [])
            if not items:
                break

            for item in items:
                ts_unix = item.get("display_time") or item.get("created_at") or 0
                try:
                    ts = pd.to_datetime(int(ts_unix), unit="s", utc=True).tz_convert("Asia/Shanghai")
                except Exception:
                    ts = pd.NaT

                # 优先用 title，没有则用 content_text / content（去 HTML 标签）
                title = str(item.get("title") or "").strip()
                body = str(item.get("content_text") or item.get("content") or "").strip()
                body = re.sub(r"<[^>]+>", "", body)  # 兜底去 HTML
                text = f"{title}　{body}".strip() if body and body != title else (title or body)

                rows.append({
                    "标题": text[:300],
                    "发布时间": ts,
                    "来源": "华尔街见闻",
                })

            # 取最旧一条的时间戳作为下一页 cursor
            last_ts = items[-1].get("display_time") or items[-1].get("created_at") or 0
            cursor = str(last_ts)
            _time.sleep(0.5)

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    def get_stock_news(self, ts_code: str) -> pd.DataFrame:
        """个股新闻（不缓存，按需拉取）。"""
        # akshare 使用6位代码，去掉后缀
        symbol = ts_code.split(".")[0]
        return rate_limited_call(
            "ak_stock_news",
            ak.stock_news_em,
            symbol=symbol,
        )

    # ---- 公司公告 ----

    # 高影响公告的关键词（用于后置过滤）
    _NOTICE_HIGH_IMPACT_KEYWORDS = [
        "重大", "资产重组", "风险提示", "持股", "权益变动",
        "股份质押", "收购", "增发", "配股",
    ]

    def get_company_notices(self, date: str, high_impact_only: bool = True) -> pd.DataFrame:
        """
        上市公司重大公告（全部类型）。

        Args:
            date: YYYYMMDD 格式交易日
            high_impact_only: 为 True 时只返回高影响类型（关键词过滤），默认开启。
                              akshare 的分类接口有 KeyError bug，故统一拉全量再过滤。

        Returns:
            DataFrame，含 代码、简称、公告标题、公告类型、公告日期 等字段。
        """
        # 缓存全量，过滤在内存中完成，避免缓存与过滤条件耦合
        df = cached_daily(
            name="ak_company_notices",
            date_key=date,
            fetch_fn=lambda: self._fetch_all_company_notices(date),
        )
        if df is None or df.empty or not high_impact_only:
            return df if df is not None else pd.DataFrame()
        return self._filter_high_impact_notices(df)

    def _filter_high_impact_notices(self, df: pd.DataFrame) -> pd.DataFrame:
        """对全量公告做高影响类型过滤，返回空时退回全量。"""
        type_col = next((c for c in df.columns if "类型" in c or "种类" in c), None)
        if not type_col:
            return df
        pattern = "|".join(self._NOTICE_HIGH_IMPACT_KEYWORDS)
        mask = df[type_col].str.contains(pattern, na=False)
        filtered = df[mask]
        return filtered if not filtered.empty else df

    @_RETRY
    def _fetch_all_company_notices(self, date: str) -> pd.DataFrame:
        """拉取全量公告（含所有类型），结果全量缓存。"""
        df = rate_limited_call(
            "ak_company_notices",
            ak.stock_notice_report,
            symbol="全部",
            date=date,
        )
        return df if df is not None else pd.DataFrame()

    # ---- 券商研报 ----

    def get_research_reports(self, ts_codes: list[str], max_days: int = 3) -> pd.DataFrame:
        """
        批量拉取个股券商研报（最近 max_days 天）。

        Args:
            ts_codes: Tushare 格式股票代码列表，如 ['000001.SZ', '600000.SH']
            max_days: 只保留最近 N 天的研报，过滤掉旧数据

        Returns:
            DataFrame，含 股票代码、报告名称、东财评级、机构、日期 等字段，
            按日期降序排列，去重后最多返回 30 条。
        """
        if not ts_codes:
            return pd.DataFrame()

        cutoff = datetime.date.today() - datetime.timedelta(days=max_days)
        frames = []
        for ts_code in ts_codes[:15]:  # 最多查15只，控制请求量
            symbol = ts_code.split(".")[0]
            try:
                df = rate_limited_call(
                    "ak_research_report",
                    ak.stock_research_report_em,
                    symbol=symbol,
                )
                if df is not None and not df.empty:
                    df["_stock_code"] = ts_code
                    frames.append(df)
            except Exception as e:
                logger.debug("研报拉取失败 %s: %s", ts_code, e)

        if not frames:
            return pd.DataFrame()

        combined = pd.concat(frames, ignore_index=True)
        date_col = next((c for c in combined.columns if "日期" in c or "时间" in c), None)
        if date_col:
            combined["_date"] = pd.to_datetime(combined[date_col], errors="coerce").dt.date
            combined = combined[combined["_date"] >= cutoff]
            combined = combined.sort_values("_date", ascending=False)

        return combined.drop_duplicates(subset=["报告名称"] if "报告名称" in combined.columns else None).head(30)

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
