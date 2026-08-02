"""L1 市场内部资金适配器（决定"水流到哪"）。

三条 2026-08-02 定档的正确性要求全部落在本文件：
(a) 北向只取 2024-08-19 之后——不止统计截断，**值本身也不入库**：断点前是净买入、
    断点后是成交额，把 -67亿(净卖出) 存进"北向成交额"列，回看模式会显示一个口径错误的数；
(b) margin_ratio 分子(融资余额·T+1发布)与分母(流通市值·T日出)必须**同一交易日 X** 相除，
    比值的可见时点由较晚发布的一腿决定(lag_days=1)——绝不拿不同日的两个数相除；
(c) 解禁是前瞻计划、未来值没有历史分布可比 → 展示项(no_dist=1·direction=0·不算分位)。

各类数值守卫沿用 base：多所汇总缺一即弃当日（不部分求和）、分页取满、单位换算就地注释。
"""

from __future__ import annotations

import logging
import re

import pandas as pd

from app.macro.adapters.base import Point, paged_fetch, to_ymd

logger = logging.getLogger(__name__)


def _pro():
    from app.macro.adapters.ts_rates import _pro as p
    return p()


def _prov():
    from app.data.composite_provider import CompositeProvider
    return CompositeProvider()


# ──────────────────────────────────────────────
# 两市成交额（综指口径·2 次 range 调用）
# ──────────────────────────────────────────────

class TurnoverAdapter:
    """两市成交额 = 上证综指 + 深证综指 amount（千元→亿元）。

    口径：**沪深两市·不含北交所**（新闻口径"两市成交X万亿"即此）。
    实测 20260731：综指合计 25419 亿 vs 全A(含BJ) 25599 亿，差 0.7%——为省去逐日全市场
    行情聚合(回补要 900+ 次调用)，采用综指口径并如实标注。
    """

    name = "tushare:index_daily(综指)"
    codes = ("turnover_total",)
    lookback_days = 10

    def fetch(self, start: str, end: str) -> list[Point]:
        sh = _prov().get_index_daily_range("000001.SH", start, end)
        sz = _prov().get_index_daily_range("399106.SZ", start, end)
        if sh is None or sh.empty or sz is None or sz.empty:
            return []
        m = (sh[["trade_date", "amount"]].rename(columns={"amount": "sh"})
             .merge(sz[["trade_date", "amount"]].rename(columns={"amount": "sz"}),
                    on="trade_date", how="inner"))          # inner：缺任一所即弃当日
        m["yi"] = (pd.to_numeric(m["sh"], errors="coerce")
                   + pd.to_numeric(m["sz"], errors="coerce")) / 1e5   # 千元→亿元
        return [Point("turnover_total", to_ymd(r.trade_date), float(r.yi), self.name)
                for r in m.itertuples() if pd.notna(r.yi)]


# ──────────────────────────────────────────────
# 两融三指标（margin ×3 交易所 + daily_basic 分母）
# ──────────────────────────────────────────────

def margin_complete_by_date(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """三所融资数据按日对齐，**只保留三所齐全的日期**（纯函数·可单测）。

    ⚠️实测事故：20260731 仅 SSE 有数据(SZSE/BSE 未发布)，裸 sum 得 13274 亿(实际约 25845 亿)
    = -49% 假暴跌。判据只看"该所当日有没有行"，**不看数值大小**——北交所两融余额本来就只有
    几十亿，用阈值判缺失必然误杀真实小值。被弃日期由 align 的结转规则接手(沿用昨值·标 stale)。
    """
    parts = []
    for ex, df in frames.items():
        if df is None or df.empty:
            return pd.DataFrame()                # 整所缺失 → 无法保证任何日期齐全
        d = df[["trade_date", "rzye", "rzmre"]].copy()
        d["trade_date"] = d["trade_date"].map(to_ymd)
        parts.append(d.rename(columns={"rzye": f"rzye_{ex}", "rzmre": f"rzmre_{ex}"}))
    m = parts[0]
    for x in parts[1:]:
        m = m.merge(x, on="trade_date", how="outer")
    complete = m.dropna(subset=[c for c in m.columns if c.startswith("rzye_")])
    dropped = sorted(set(m.trade_date) - set(complete.trade_date))
    if dropped:
        logger.warning("[macro] margin 有 %d 日三所不齐(如 %s)·按缺数处理不部分求和",
                       len(dropped), dropped[-3:])
    out = pd.DataFrame({"trade_date": complete["trade_date"]})
    out["rzye"] = sum(pd.to_numeric(complete[f"rzye_{ex}"], errors="coerce") for ex in frames)
    out["rzmre"] = sum(pd.to_numeric(complete[f"rzmre_{ex}"], errors="coerce").fillna(0)
                       for ex in frames)         # 买入额缺列不致命·余额三所必须齐
    return out.sort_values("trade_date")


class MarginAdapter:
    """融资余额(绝对值·亿) / 融资余额÷流通市值(%·主指标) / 融资买入额÷两市成交额(%)。

    (b) 同日对齐：ratio(X) = margin(X)/circ_mv(X)，两腿都取交易日 X；margin(X) 于 X+1 晨发布，
    故三个指标 lag_days=1——比值在 X+1 才可见，align 层由此保证"不拿不同日的数相除"。
    分母 = Σ daily_basic.circ_mv(全A·万元→亿)，与 2025 均值 2.41% 的用户参考值验证吻合(实测 2.40%)。
    """

    name = "tushare:margin+daily_basic"
    codes = ("margin_balance", "margin_ratio", "margin_buy_ratio")
    lookback_days = 7                            # 分母按日读 parquet·增量只需近几日

    _EXCHANGES = ("SSE", "SZSE", "BSE")

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        frames = {ex: rate_limited_call("tushare_margin", _pro().margin,
                                        start_date=start, end_date=end, exchange_id=ex)
                  for ex in self._EXCHANGES}
        m = margin_complete_by_date(frames)
        if m.empty:
            return []
        pts: list[Point] = []
        turn = self._turnover_map(start, end)
        prov = _prov()
        for r in m.itertuples():
            x = r.trade_date
            bal_yi = float(r.rzye) / 1e8
            pts.append(Point("margin_balance", x, round(bal_yi, 1), self.name))
            circ = self._circ_mv_yi(prov, x)
            if circ:                             # 分母缺该日 → ratio 当日无点(不跨日相除)
                pts.append(Point("margin_ratio", x, round(bal_yi / circ * 100, 4), self.name))
            t = turn.get(x)
            if t:
                pts.append(Point("margin_buy_ratio", x,
                                 round(float(r.rzmre) / 1e8 / t * 100, 4), self.name))
        return pts

    def _turnover_map(self, start: str, end: str) -> dict[str, float]:
        return {p.as_of: p.value for p in TurnoverAdapter().fetch(start, end)}

    @staticmethod
    def _circ_mv_yi(prov, date: str) -> float | None:
        """全A流通市值合计(亿)。取不到该日就返回 None——绝不用别的日期顶替。"""
        try:
            db = prov.get_daily_basic(date)
            if db is None or db.empty or "circ_mv" not in db.columns:
                return None
            v = pd.to_numeric(db["circ_mv"], errors="coerce").sum() / 1e4   # 万元→亿
            return float(v) if v > 0 else None
        except Exception as e:
            logger.debug("[macro] daily_basic(%s) 不可得·margin_ratio 当日跳过: %s", date, e)
            return None


# ──────────────────────────────────────────────
# 北向成交额（语义断点·只取断点之后）
# ──────────────────────────────────────────────

class NorthboundAdapter:
    """北向成交额(亿·非净额)。moneyflow_hsgt 硬上限 300 行/次 → offset 分页。

    (a) **断点前的行不入库**：2024-08-19 前 north_money=净买入(可为负)、之后=成交额。
    truncate 只管统计窗口，值本身若入库，回看 2024-06 会显示"北向成交额 -67亿"——口径错误。
    单位：百万元→亿(/100)。分位在断点后段内算，截至 2026-07 约 480 样本 ≥ 250 门槛。
    """

    name = "tushare:moneyflow_hsgt"
    codes = ("northbound_turnover",)
    lookback_days = 10
    _BREAK = "20240819"
    _PAGE = 300

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        lo = max(start, self._BREAK)
        if lo > end:
            return []
        df = paged_fetch(
            lambda off: rate_limited_call("tushare_hsgt", _pro().moneyflow_hsgt,
                                          start_date=lo, end_date=end,
                                          limit=self._PAGE, offset=off),
            page=self._PAGE)
        if df.empty:
            return []
        df = df.drop_duplicates("trade_date")
        v = pd.to_numeric(df["north_money"], errors="coerce") / 100    # 百万元→亿
        return [Point("northbound_turnover", to_ymd(d), float(x), self.name)
                for d, x in zip(df["trade_date"], v)
                if pd.notna(x) and to_ymd(d) >= self._BREAK]


# ──────────────────────────────────────────────
# 全市场主力净流入（大盘直连口径·1 次 range）
# ──────────────────────────────────────────────

class MainflowAdapter:
    """全市场主力净流入(亿·沪深)。`moneyflow_mkt_dc` 单位为**元**(/1e8→亿)。

    口径验证(2026-08-02 实测·与项目 market_fund 模块的 Σmoneyflow_dc·剔.BJ 口径逐日对比)：
    07-29 两者均 -119.5 亿 / 07-30 均 -789.9 亿 / 07-31 均 +625.4 亿——**完全吻合**。
    即 mkt_dc 是同一份东财大盘口径的市场级直出，1 次 range 调用免去 900+ 次逐日聚合。
    """

    name = "tushare:moneyflow_mkt_dc"
    codes = ("mainflow_total",)
    lookback_days = 10

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        df = rate_limited_call("tushare_mkt_dc", _pro().moneyflow_mkt_dc,
                               start_date=start, end_date=end)
        if df is None or df.empty or "net_amount" not in df.columns:
            return []
        v = pd.to_numeric(df["net_amount"], errors="coerce") / 1e8     # 元→亿
        return [Point("mainflow_total", to_ymd(d), round(float(x), 1), self.name)
                for d, x in zip(df["trade_date"], v) if pd.notna(x)]


# ──────────────────────────────────────────────
# 宽基 ETF 份额变化
# ──────────────────────────────────────────────

# 宽基篮子：**指数名+ETF 结尾**才算。裸关键词匹配会混入"创业板新能源ETF/沪深300自由现金流ETF"
# 这类行业与Smart-Beta产品(实测污染 379→收紧后 129 只·残留污染 0)——那些的申赎是行业观点，
# 不是"场外资金借道宽基入场"的信号。
_BROAD_PAT = re.compile(
    r"(?:沪深300|中证500|中证1000|中证2000|上证50|创业板|科创50|科创板50|中证A500|A500)ETF(?:基金)?$")


def etf_share_delta(shares: pd.DataFrame) -> pd.Series:
    """逐基金份额差分后按日求和（纯函数·可单测）。

    **只累计"前后两日都存在"的基金的 Δ**：新上市 ETF 首日份额若直接进总量差分，
    会制造一根与申赎无关的假跳变。列：ts_code/trade_date/fd_share(万份)。返回单位：亿份。
    """
    if shares is None or shares.empty:
        return pd.Series(dtype=float)
    p = shares.pivot_table(index="trade_date", columns="ts_code",
                           values="fd_share", aggfunc="last").sort_index()
    return p.diff().sum(axis=1, min_count=1) / 1e4          # 万份→亿份；diff 首日自然为 NaN


class EtfShareAdapter:
    """宽基 ETF 份额日变化(亿份)。篮子 = 场内基金中名称含宽基指数关键词的 ETF。

    取数双路（同一数据两种切法）：区间 >30 天走"逐基金 range"(~篮子数次调用·回补快)；
    短区间走"逐日全表"(1 次/日·增量便宜)。
    """

    name = "tushare:fund_share"
    codes = ("etf_share_chg",)
    lookback_days = 7

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        basket = self._basket()
        if not basket:
            return []
        span = (pd.Timestamp(end) - pd.Timestamp(start)).days
        frames = []
        if span > 30:
            for code in basket:
                df = rate_limited_call("tushare_fund_share", _pro().fund_share,
                                       ts_code=code, start_date=start, end_date=end)
                if df is not None and not df.empty:
                    frames.append(df[["ts_code", "trade_date", "fd_share"]])
        else:
            for d in pd.date_range(start, end):
                df = rate_limited_call("tushare_fund_share", _pro().fund_share,
                                       trade_date=d.strftime("%Y%m%d"))
                if df is not None and not df.empty:
                    frames.append(df[df["ts_code"].isin(basket)]
                                  [["ts_code", "trade_date", "fd_share"]])
        if not frames:
            return []
        allf = pd.concat(frames, ignore_index=True)
        allf["trade_date"] = allf["trade_date"].map(to_ymd)
        delta = etf_share_delta(allf)
        return [Point("etf_share_chg", str(d), round(float(v), 2), self.name)
                for d, v in delta.items() if pd.notna(v)]

    @staticmethod
    def _basket() -> list[str]:
        from app.data.cache import rate_limited_call
        fb = rate_limited_call("tushare_fund_basic", _pro().fund_basic, market="E")
        if fb is None or fb.empty:
            return []
        hit = fb[fb["name"].astype(str).str.contains(_BROAD_PAT)]
        return sorted(hit["ts_code"].unique().tolist())


# ──────────────────────────────────────────────
# 新成立基金份额（近4周滚动）
# ──────────────────────────────────────────────

def newfund_rolling(fb: pd.DataFrame, start: str, end: str, window_days: int = 28) -> pd.Series:
    """按成立日聚合股票+混合型发行份额(亿份)，近 window_days 自然日滚动求和（纯函数）。"""
    if fb is None or fb.empty:
        return pd.Series(dtype=float)
    eq = fb[fb["fund_type"].isin(["股票型", "混合型"])].copy()
    eq["found_date"] = eq["found_date"].astype(str).str.replace("-", "")
    eq = eq[eq["found_date"].str.len() == 8]
    daily = (pd.to_numeric(eq["issue_amount"], errors="coerce")
             .groupby(eq["found_date"]).sum())
    idx = pd.date_range(pd.Timestamp(start) - pd.Timedelta(days=window_days), end)
    s = daily.reindex(idx.strftime("%Y%m%d")).fillna(0.0)
    roll = s.rolling(window_days).sum()
    roll = roll[roll.index >= start]
    return roll.dropna()


class NewFundAdapter:
    """新成立基金份额(近4周·亿份)。fund_basic 场内+场外(O 表 15000 行=上限·分页取满)。

    口径：股票型+混合型 issue_amount 按 found_date 聚合、28 自然日(≈4周)滚动。
    末端 2-3 日可能因公告入库滞后而低估——每晚 --resync 窗口重写近旬即自动修正。
    """

    name = "tushare:fund_basic"
    codes = ("new_fund_share",)
    lookback_days = 7
    _PAGE = 5000

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        frames = []
        for mkt in ("E", "O"):
            df = paged_fetch(
                lambda off, m=mkt: rate_limited_call(
                    "tushare_fund_basic", _pro().fund_basic,
                    market=m, limit=self._PAGE, offset=off),
                page=self._PAGE)
            if not df.empty:
                frames.append(df[["ts_code", "fund_type", "found_date", "issue_amount"]])
        if not frames:
            return []
        roll = newfund_rolling(pd.concat(frames, ignore_index=True), start, end)
        return [Point("new_fund_share", str(d), round(float(v), 1), self.name)
                for d, v in roll.items()]


# ──────────────────────────────────────────────
# 解禁规模（前瞻·展示项）
# ──────────────────────────────────────────────

def float_release_value(events: pd.DataFrame, close_map: dict[str, float],
                        date: str, horizon_days: int = 28) -> float | None:
    """date 视角·未来4周(28自然日)解禁市值(亿) = Σ close(date) × float_share万股 /1e4（纯函数）。

    (c) 这是**前瞻计划**不是已发生事实：未来值没有历史分布可比 → 不算分位不评分(no_dist)。
    无价格的事件跳过；一条价格都配不上 → None(不给假0)。
    """
    if events is None or events.empty:
        return None
    hi = (pd.Timestamp(date) + pd.Timedelta(days=horizon_days)).strftime("%Y%m%d")
    win = events[(events["float_date"] > date) & (events["float_date"] <= hi)]
    if win.empty:
        return 0.0                               # 窗口内真没有解禁 → 真0(与配不上价格不同)
    total, matched = 0.0, 0
    for r in win.itertuples():
        px = close_map.get(r.ts_code)
        if px is None or pd.isna(r.float_share):
            continue
        # ⚠️float_share 实测单位是【股】·不是文档标注的万股——交叉验证：920180.BJ
        # float_share=300000·float_ratio=0.254% → 反推总股本1.18亿股(自洽)；
        # 按"万股"反推总股本=1.18万亿股(荒谬)。错按万股算会虚高1e4倍(实测得出1300万亿的假数)。
        total += float(px) * float(r.float_share) / 1e8     # 股×元 → 亿元
        matched += 1
    return round(total, 1) if matched else None


class FloatReleaseAdapter:
    """解禁规模(未来4周·亿)。share_float 单次 6000 行=上限 → offset 分页。

    只算区间尾部 ≤60 个有行情缓存的交易日（展示项无需长历史；60 天足够画 sparkline），
    以免回补拖 900+ 次全市场行情。
    """

    name = "tushare:share_float"
    codes = ("float_release",)
    lookback_days = 0
    _PAGE = 6000
    _MAX_DAYS = 60

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        from app.macro.adapters.base import sliced_fetch
        # 事件窗只需覆盖"最近60个计算日 + 未来45天"，不跟随回补起点拉4年全量。
        # ⚠️两层生产实测的取数限制叠加：share_float 是(股票×股东)明细粒度·6个月窗超10万行，
        # 而其 offset 全局上限≈10万(超过报"查询数据失败，请确认参数")→ **按月切片**让每片
        # 行数~2万·片内 offset 从0起·彻底绕开上限。
        lo, hi = pd.Timestamp(end) - pd.Timedelta(days=140), pd.Timestamp(end) + pd.Timedelta(days=45)
        # 15天一片：解禁季单月也超 offset≈10万上限(生产实测 6/13~7/12 单月片仍报错)
        frames, cur = [], lo
        while cur <= hi:
            seg_end = min(cur + pd.Timedelta(days=14), hi)
            try:
                chunk = paged_fetch(
                    lambda off, a=cur, b=seg_end: rate_limited_call(
                        "tushare_share_float", _pro().share_float,
                        start_date=a.strftime("%Y%m%d"), end_date=b.strftime("%Y%m%d"),
                        limit=self._PAGE, offset=off),
                    page=self._PAGE)
                if not chunk.empty:
                    frames.append(chunk)
            except Exception as e:
                logger.warning("[macro] share_float 片 %s~%s 失败(该片事件缺失·值可能低估): %s",
                               cur.date(), seg_end.date(), e)
            cur = seg_end + pd.Timedelta(days=1)
        ev = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if ev.empty:
            return []
        # 按(股票·解禁日·股东·股数)去重：同一事件多行(如 603284 同日同股东重复4行)会虚增合计
        keep = [c for c in ("ts_code", "float_date", "holder_name", "float_share") if c in ev.columns]
        ev = ev.drop_duplicates(subset=keep)[["ts_code", "float_date", "float_share"]].copy()
        ev["float_date"] = ev["float_date"].map(to_ymd)
        prov = _prov()
        pts: list[Point] = []
        from app.macro.sync import trading_days
        days = trading_days(start, end)[-self._MAX_DAYS:]
        for x in days:
            try:
                daily = prov.get_daily(x)
                close_map = dict(zip(daily["ts_code"],
                                     pd.to_numeric(daily["close"], errors="coerce")))
            except Exception:
                continue                          # 该日无行情缓存 → 跳过(不猜价格)
            v = float_release_value(ev, close_map, x)
            if v is not None:
                pts.append(Point("float_release", x, v, self.name))
        return pts
