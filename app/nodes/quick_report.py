"""
快讯报告生成器（Quick Report）。

支持三个时段，每个时段只分析对应时间窗口内的信息：
  - pre   盘前 (~9:00)：昨日15:00 → 今日9:00，隔夜消息+今日方向
  - mid   盘中半天 (~12:00)：09:00 → 12:00，上午催化+午后策略
  - post  盘后 (~16:05)：当日全天，复盘+明日布局

信息源（三路合并）：
  - [新闻] 财联社电报 / 东方财富财经快讯
  - [公告] akshare stock_notice_report（重大事项/资产重组/持股变动/风险提示）
  - [研报] akshare stock_research_report_em（当日观察池个股的近3日券商研报）

核心设计原则：
  - 所有信息来源必须标注 [新闻]/[公告]/[研报] 标签
  - LLM输出必须有明确的多空方向和操作逻辑
  - 报告精确到分钟，反映当时信息状态
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Literal

import pandas as pd

from app.config import get_settings
from app.data.akshare_provider import AkshareProvider

logger = logging.getLogger(__name__)

SessionType = Literal["pre", "mid", "post"]

# 每个时段的中文名称和新闻时间窗口（小时偏移，相对于当天0点）
_SESSION_META = {
    "pre":  {"label": "盘前",   "start_h": -9,  "end_h": 9},    # 昨日15:00到今日9:00
    "mid":  {"label": "盘中",   "start_h": 9,   "end_h": -1},   # 09:00 → 运行时刻（动态）
    "post": {"label": "盘后",   "start_h": 9,   "end_h": 17},
}


def build_quick_report(
    session: SessionType,
    trade_date: str | None = None,
    label_suffix: str = "",
) -> tuple[str, str, str]:
    """
    生成指定时段的快讯报告。

    Args:
        session: "pre" | "mid" | "post"
        trade_date: YYYYMMDD，默认今日
        label_suffix: 标签后缀，如"速报"/"完整版"，用于区分同时段多次推送

    Returns:
        (filepath, title, content) — Markdown 内容，已写入文件
    """
    settings = get_settings()
    now = datetime.datetime.now()
    today = trade_date or now.strftime("%Y%m%d")
    now_str = now.strftime("%Y-%m-%d %H:%M")
    meta = dict(_SESSION_META[session])
    if label_suffix:
        meta["label"] = f"{meta['label']}{label_suffix}"

    # ---- 拉取三路信息源 ----
    news_df = _fetch_news(today, meta["start_h"], meta["end_h"])
    notices_text = _fetch_notices(today)
    research_text = _fetch_research_reports(today)

    has_content = not news_df.empty or notices_text or research_text

    # ---- 无任何内容时直接返回提示，拒绝生成 ----
    if not has_content:
        content = (
            f"## ⚠️ {meta['label']}快讯暂无内容\n\n"
            f"> 当前时间窗口（{_window_desc(meta['start_h'], meta['end_h'])}）内"
            f"三路信息源（财联社/公告/研报）均无新内容。\n>\n"
            f"> 本系统拒绝在无真实信息来源的情况下生成分析，"
            f"以免干扰您的判断。\n\n"
            f"请稍后刷新或等待下一时段报告。"
        )
        filepath = settings.report_dir / f"{today}_{now.strftime('%H%M')}_{session}.md"
        filepath.write_text(f"# A股{meta['label']}快讯\n> 📅 **{now_str}**\n\n" + content, encoding="utf-8")
        logger.warning("[快讯] 三路信息源均为空，已生成空报告: %s", filepath)
        title = f"【{meta['label']}】{today[4:6]}/{today[6:]} | 暂无新消息"
        return str(filepath), title, content

    # ---- 调用 LLM 生成分析 ----
    content = _generate_analysis(
        session, meta["label"], now_str, news_df, today,
        notices_text=notices_text,
        research_text=research_text,
    )

    # ---- 保存文件 ----
    filename = f"{today}_{now.strftime('%H%M')}_{session}.md"
    filepath = settings.report_dir / filename
    news_count = len(news_df)
    header = (
        f"# A股{meta['label']}快讯\n"
        f"> 📅 **{now_str}**　|　新闻: {news_count} 条"
        f"　|　公告: {'有' if notices_text else '无'}"
        f"　|　研报: {'有' if research_text else '无'}\n"
        f"> ⚠️ LLM信息聚合，不构成投资建议\n\n"
    )
    filepath.write_text(header + content, encoding="utf-8")
    logger.info("[快讯] 报告已保存: %s", filepath)

    title = f"【{meta['label']}】{today[4:6]}/{today[6:]} | {_headline(content)}"
    return str(filepath), title, header + content


# --------------------------------------------------------------------------- #
# 内部实现
# --------------------------------------------------------------------------- #

def _fetch_notices(today: str) -> str:
    """
    拉取当日重大公告并格式化为文本块。
    返回空字符串表示当日无公告。
    """
    try:
        ak_provider = AkshareProvider()
        df = ak_provider.get_company_notices(today)
        if df is None or df.empty:
            return ""

        # 动态找列名
        code_col = next((c for c in df.columns if "代码" in c or "股票" in c), None)
        name_col = next((c for c in df.columns if "简称" in c or "名称" in c), None)
        title_col = next((c for c in df.columns if "标题" in c or "公告" in c), None)
        type_col = next((c for c in df.columns if "类型" in c or "种类" in c), None)

        lines = []
        for _, row in df.head(20).iterrows():
            parts = []
            if code_col:
                parts.append(str(row.get(code_col, "")).strip())
            if name_col:
                parts.append(str(row.get(name_col, "")).strip())
            if type_col:
                parts.append(f"[{str(row.get(type_col, '')).strip()}]")
            if title_col:
                parts.append(str(row.get(title_col, "")).strip()[:80])
            if parts:
                lines.append(" ".join(parts))

        return "\n".join(lines) if lines else ""
    except Exception as e:
        logger.warning("[快讯] 公告拉取失败: %s", e)
        return ""


def _fetch_research_reports(today: str) -> str:
    """
    拉取最近3天针对观察池个股的券商研报，格式化为文本块。
    观察池从 strategy.db 的 watchlist 读取，最多取前15只。
    返回空字符串表示无近期研报。
    """
    try:
        from app.strategy.forward_tracker import get_recent_watchlist_perf
        recent_list = get_recent_watchlist_perf(trade_date=today, days=5)
        if not recent_list:
            return ""

        # get_recent_watchlist_perf 返回 list[dict]，提取 ts_code 字段
        ts_codes = list({item["ts_code"] for item in recent_list if "ts_code" in item})[:15]
        if not ts_codes:
            return ""

        ak_provider = AkshareProvider()
        df = ak_provider.get_research_reports(ts_codes, max_days=3)
        if df is None or df.empty:
            return ""

        # 字段名以真实 akshare 返回为准：报告名称、东财评级、机构、日期
        lines = []
        for _, row in df.head(20).iterrows():
            code = str(row.get("_stock_code", row.get("股票代码", ""))).strip()
            name = str(row.get("股票简称", "")).strip()
            org = str(row.get("机构", "")).strip()
            rating = str(row.get("东财评级", "")).strip()
            report = str(row.get("报告名称", "")).strip()[:80]
            date = str(row.get("日期", ""))[:10]
            parts = [p for p in [code, name, f"({org})" if org else "", rating, report, date] if p]
            if parts:
                lines.append(" ".join(parts))

        return "\n".join(lines) if lines else ""
    except Exception as e:
        logger.warning("[快讯] 研报拉取失败: %s", e)
        return ""


def _fetch_news(today: str, start_h: int, end_h: int) -> pd.DataFrame:
    """
    拉取财联社新闻并按时间窗口过滤。
    start_h < 0 表示取昨日（如 -9 表示昨日 15:00）。
    """
    ak = AkshareProvider()
    frames = []

    # 财联社新闻（今日）
    try:
        df_cls = ak.get_cls_news(today)
        if not df_cls.empty:
            frames.append(df_cls)
    except Exception as e:
        logger.warning("拉取财联社今日新闻失败: %s", e)

    # 华尔街见闻快讯（今日）
    try:
        df_wscn = ak.get_wscn_lives(today)
        if not df_wscn.empty:
            frames.append(df_wscn)
            logger.info("华尔街见闻快讯: 获取 %d 条", len(df_wscn))
    except Exception as e:
        logger.warning("拉取华尔街见闻失败: %s", e)

    # 需要昨日新闻（盘前报告）
    if start_h < 0:
        yesterday = _offset_date(today, -1)
        try:
            df_yest = ak.get_cls_news(yesterday)
            if not df_yest.empty:
                frames.append(df_yest)
        except Exception as e:
            logger.warning("拉取财联社昨日新闻失败: %s", e)
        try:
            df_wscn_yest = ak.get_wscn_lives(yesterday)
            if not df_wscn_yest.empty:
                frames.append(df_wscn_yest)
        except Exception as e:
            logger.warning("拉取华尔街见闻昨日新闻失败: %s", e)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return df

    # 解析时间并过滤窗口
    time_col = "发布时间" if "发布时间" in df.columns else df.columns[1]
    df["_ts"] = pd.to_datetime(df[time_col], errors="coerce", utc=True).dt.tz_convert("Asia/Shanghai")

    tz_sh = datetime.timezone(datetime.timedelta(hours=8))
    today_dt = datetime.datetime.strptime(today, "%Y%m%d")
    if start_h >= 0:
        start_ts = today_dt.replace(hour=start_h, minute=0, tzinfo=tz_sh)
    else:
        # start_h 为负，表示前一天
        prev = today_dt - datetime.timedelta(days=1)
        start_ts = prev.replace(hour=24 + start_h, minute=0, tzinfo=tz_sh)

    # end_h == -1 为哨兵值，表示动态取当前时刻（mid 时段使用）
    if end_h == -1:
        end_ts = datetime.datetime.now(tz=tz_sh)
    else:
        # 精确到整点，不含下一分钟（避免盘前窗口漏进开盘后的新闻）
        end_ts = today_dt.replace(hour=end_h, minute=0, second=0, tzinfo=tz_sh)

    mask = (df["_ts"] >= start_ts) & (df["_ts"] <= end_ts)
    filtered = df[mask].sort_values("_ts", ascending=False).reset_index(drop=True)
    return filtered


def _board_limit_pct(ts_code: str, name: str) -> float:
    """
    返回个股的涨跌停幅度（百分比），用于精确判断涨停/跌停。
      - ST/*ST 股：5%
      - 北交所（.BJ）：30%
      - 创业板(300/301)、科创板(688)：20%
      - 主板（600/601/603/605/000/001/002/003）：10%
    """
    if "ST" in str(name).upper():
        return 5.0
    if ts_code.endswith(".BJ"):
        return 30.0
    code = ts_code.split(".")[0]
    if code.startswith(("300", "301", "688")):
        return 20.0
    return 10.0


def _count_limit_moves(df_daily: pd.DataFrame, code2name: dict[str, str]) -> tuple[int, int]:
    """
    板块感知地统计收盘涨停/跌停家数。
    判定：pct_chg 落在 [limit-0.3, limit+0.5] 区间内（排除新股无涨跌幅限制的极端值）。
    """
    limit_up = limit_down = 0
    for _, r in df_daily.iterrows():
        ts_code = r["ts_code"]
        pct = r.get("pct_chg")
        if pd.isna(pct):
            continue
        limit = _board_limit_pct(ts_code, code2name.get(ts_code, ""))
        # 收盘涨停：接近正向涨停幅度（容差防四舍五入），且不超过太多（排除新股）
        if limit - 0.3 <= pct <= limit + 0.5:
            limit_up += 1
        elif -(limit + 0.5) <= pct <= -(limit - 0.3):
            limit_down += 1
    return limit_up, limit_down


def _crowding_flag(pct: float, turnover: float, vol_ratio: float) -> str:
    """
    Phase C：个股拥挤度/追高风险标记。
      🔥过热：涨幅大 + 换手高 + 放量（题材高潮，追高风险大）
      ⚠️高换手：换手率过高（分歧剧烈）
      ✅健康：温和放量上涨
    """
    if pct >= 7 and turnover >= 10 and vol_ratio >= 1.5:
        return "🔥过热"
    if turnover >= 18:
        return "⚠️高换手"
    if pct >= 5 and turnover >= 8:
        return "⚠️偏热"
    return "✅健康"


_NEGATIVE_KEYWORDS = [
    "立案", "处罚", "警示函", "问询函", "监管措施", "违规", "违法", "诉讼", "仲裁",
    "减持", "拟减持", "退市", "风险警示", "*ST", "ST", "业绩预减", "业绩预亏",
    "商誉减值", "质押", "冻结", "平仓", "被执行", "失信", "停牌核查",
]


def _negative_events(today: str, provider) -> dict[str, str]:
    """
    扫描当日全量公告，识别个股负面事件（立案/减持/问询/退市等），用于避雷。
    返回 {6位代码: 命中的负面关键词}。
    """
    events: dict[str, str] = {}
    try:
        df = provider.get_company_notices(today, high_impact_only=False) \
            if hasattr(provider, "get_company_notices") else None
    except TypeError:
        df = provider.get_company_notices(today)
    except Exception:
        df = None
    if df is None or df.empty:
        return events

    code_col = next((c for c in df.columns if "代码" in c), None)
    title_col = next((c for c in df.columns if "标题" in c or "公告" in c), None)
    type_col = next((c for c in df.columns if "类型" in c), None)
    if not code_col:
        return events

    for _, row in df.iterrows():
        text = " ".join(str(row.get(c, "")) for c in (title_col, type_col) if c)
        hit = next((kw for kw in _NEGATIVE_KEYWORDS if kw in text), None)
        if hit:
            code6 = str(row.get(code_col, "")).zfill(6)
            # 同股多条只记最严重的（立案/退市/处罚优先）
            if code6 not in events or hit in ("立案", "退市", "处罚", "违法"):
                events[code6] = hit
    return events


def _enrich_candidates(
    today: str,
    provider,
    df_daily: pd.DataFrame,
    df_mf: pd.DataFrame | None,
    code2name: dict[str, str],
    code2ind: dict[str, str],
) -> str:
    """
    Phase C+D：个股量化画像。
    对今日重点候选股（涨幅Top+超大单Top+成交额Top 并集）补充：
    换手率/量比（daily_basic）、千股千评（综合得分/机构参与度/人气排名）、拥挤度标记。
    """
    # 候选池：三榜并集
    cand = set()
    try:
        cand |= set(df_daily[df_daily["pct_chg"] <= 21].nlargest(12, "pct_chg")["ts_code"])
        cand |= set(df_daily.nlargest(12, "amount")["ts_code"])
    except Exception:
        pass
    if df_mf is not None and not df_mf.empty and "elg_net" in df_mf.columns:
        cand |= set(df_mf.nlargest(12, "elg_net")["ts_code"])
    if not cand:
        return ""

    # daily_basic：换手率、量比
    db_map = {}
    try:
        db = provider.get_daily_basic(today)
        if db is not None and not db.empty:
            for _, r in db[db["ts_code"].isin(cand)].iterrows():
                db_map[r["ts_code"]] = (r.get("turnover_rate"), r.get("volume_ratio"))
    except Exception as e:
        logger.debug("[画像] daily_basic失败: %s", e)

    # 千股千评：综合得分、机构参与度、人气排名（akshare，6位代码）
    cmt_map = {}
    try:
        cmt = provider.get_stock_comment(today)
        if cmt is not None and not cmt.empty:
            code_col = next((c for c in cmt.columns if c == "代码"), None)
            for _, r in cmt.iterrows():
                code6 = str(r.get(code_col, "")).zfill(6)
                cmt_map[code6] = (r.get("综合得分"), r.get("机构参与度"), r.get("目前排名"))
    except Exception as e:
        logger.debug("[画像] 千股千评失败: %s", e)

    daily_idx = df_daily.set_index("ts_code")
    mf_idx = df_mf.set_index("ts_code") if df_mf is not None and not df_mf.empty else None

    rows = []
    for ts in cand:
        if ts not in daily_idx.index:
            continue
        d = daily_idx.loc[ts]
        pct = float(d["pct_chg"]) if pd.notna(d["pct_chg"]) else 0
        amt = float(d["amount"]) / 100000 if pd.notna(d["amount"]) else 0
        turnover, vol_ratio = db_map.get(ts, (None, None))
        turnover = float(turnover) if turnover is not None and pd.notna(turnover) else 0
        vol_ratio = float(vol_ratio) if vol_ratio is not None and pd.notna(vol_ratio) else 0
        elg = float(mf_idx.loc[ts, "elg_net"]) if mf_idx is not None and ts in mf_idx.index and "elg_net" in mf_idx.columns else None
        score, inst, rank = cmt_map.get(ts.split(".")[0], (None, None, None))
        rows.append({
            "ts": ts, "name": code2name.get(ts, ""), "ind": code2ind.get(ts, ""),
            "pct": pct, "amt": amt, "turnover": turnover, "vol_ratio": vol_ratio,
            "elg": elg, "score": score, "inst": inst, "rank": rank,
            "flag": _crowding_flag(pct, turnover, vol_ratio),
        })

    if not rows:
        return ""

    # 负面事件扫描（避雷：立案/减持/问询/退市等）
    neg = _negative_events(today, provider)

    # 按超大单净流入排序（主力意图优先），无则按涨幅
    rows.sort(key=lambda x: (x["elg"] if x["elg"] is not None else -999, x["pct"]), reverse=True)

    lines = ["\n========== 个股量化画像（候选池，含拥挤度+负面事件避雷） =========="]
    lines.append("（字段：涨幅 | 成交 | 换手率 | 量比 | 超大单净流入 | 千评得分 | 机构参与度 | 人气排名 | 拥挤度 | ⚠️负面）")
    for r in rows[:15]:
        elg_s = f"{r['elg']:+.1f}亿" if r["elg"] is not None else "—"
        score_s = f"{r['score']:.0f}" if r["score"] is not None and pd.notna(r["score"]) else "—"
        inst_s = f"{r['inst']*100:.0f}%" if r["inst"] is not None and pd.notna(r["inst"]) else "—"
        rank_s = f"{int(r['rank'])}" if r["rank"] is not None and pd.notna(r["rank"]) else "—"
        neg_hit = neg.get(r["ts"].split(".")[0])
        neg_s = f" | 🚨{neg_hit}" if neg_hit else ""
        lines.append(
            f"  {r['name']}({r['ts'].split('.')[0]}) {r['ind']} | "
            f"涨{r['pct']:+.1f}% 成交{r['amt']:.0f}亿 换手{r['turnover']:.1f}% 量比{r['vol_ratio']:.1f} | "
            f"超大单{elg_s} | 千评{score_s} 机构{inst_s} 人气{rank_s} | {r['flag']}{neg_s}"
        )
    if neg:
        flagged = [f"{code2name.get(c + '.SZ', code2name.get(c + '.SH', c))}({c}):{kw}"
                   for c, kw in list(neg.items())[:10]]
        lines.append("  【全市场负面事件避雷】" + "；".join(flagged))
    return "\n".join(lines)


def _lianban_stats(today: str, provider, code2name: dict[str, str]) -> dict:
    """
    计算全市场连板高度与分布（A股情绪核心指标）。
    连板=连续涨停天数。最高连板反映市场风险偏好/题材强度。

    返回 {"max_height", "distribution": {n: count}, "top_stocks": [(name,code,height)]}。
    """
    dates = _recent_trade_dates(provider, today, n=7)  # 含今日的最近7个交易日（升序）
    # 每个交易日的涨停集合（板块感知）
    limit_sets: list[set] = []
    for d in dates:
        try:
            dd = provider.get_daily(d)
            if dd is None or dd.empty:
                limit_sets.append(set())
                continue
            pct = pd.to_numeric(dd["pct_chg"], errors="coerce")
            up = set()
            for ts, p in zip(dd["ts_code"], pct):
                if pd.isna(p):
                    continue
                lim = _board_limit_pct(ts, code2name.get(ts, ""))
                if lim - 0.3 <= p <= lim + 0.5:
                    up.add(ts)
            limit_sets.append(up)
        except Exception:
            limit_sets.append(set())

    if not limit_sets or not limit_sets[-1]:
        return {"max_height": 0, "distribution": {}, "top_stocks": []}

    # 对今日涨停的每只股票，从今日往前数连续涨停天数
    today_up = limit_sets[-1]
    heights = {}
    for ts in today_up:
        h = 0
        for i in range(len(limit_sets) - 1, -1, -1):
            if ts in limit_sets[i]:
                h += 1
            else:
                break
        heights[ts] = h

    distribution: dict[int, int] = {}
    for h in heights.values():
        distribution[h] = distribution.get(h, 0) + 1

    max_height = max(heights.values()) if heights else 0
    # 最高板的代表股
    top_stocks = sorted(
        [(code2name.get(ts, ts), ts.split(".")[0], h) for ts, h in heights.items() if h == max_height],
        key=lambda x: x[0],
    )[:5]
    return {"max_height": max_height, "distribution": distribution, "top_stocks": top_stocks}


def _market_regime(
    today: str,
    provider,
    df_daily: pd.DataFrame,
    limit_up: int,
    limit_down: int,
    up_count: int,
    down_count: int,
    total_amt_yi: float,
    code2name: dict[str, str] | None = None,
) -> str:
    """
    Phase A：市场环境分桶（顶层定调）。
    综合指数趋势、市场广度、涨跌停、量能，输出阶段判断+置信度+风控建议。
    """
    parts: list[str] = ["\n========== 市场环境（顶层定调） =========="]

    # —— 1. 指数趋势（上证 + 创业板）——
    index_signals = {}
    for name, code in [("上证指数", "000001.SH"), ("创业板指", "399006.SZ")]:
        try:
            idx = provider.get_index_daily(code, today)
            if idx is None or idx.empty:
                continue
            idx = idx.sort_values("trade_date")
            close = pd.to_numeric(idx["close"], errors="coerce")
            ma5 = close.tail(5).mean()
            ma20 = close.tail(20).mean()
            ma20_prev = close.tail(21).head(20).mean() if len(close) >= 21 else ma20
            last = close.iloc[-1]
            chg = pd.to_numeric(idx["pct_chg"], errors="coerce").iloc[-1] if "pct_chg" in idx else 0
            # 近20日累计回撤（从区间高点）
            recent20 = close.tail(20)
            drawdown = (last - recent20.max()) / recent20.max() * 100
            pos_ma5 = "上方" if last > ma5 else "下方"
            pos_ma20 = "上方" if last > ma20 else "下方"
            slope = "向上" if ma20 > ma20_prev else "走平/向下"
            index_signals[name] = drawdown
            parts.append(
                f"  {name}：收{last:.0f}（{chg:+.2f}%）| MA5{pos_ma5} MA20{pos_ma20} | MA20斜率{slope}"
                f" | 距20日高点{drawdown:+.1f}%"
            )
        except Exception as e:
            logger.debug("[环境] 指数%s失败: %s", name, e)

    # —— 2. 市场广度（站上MA5/MA20占比）——
    breadth_ma5 = breadth_ma20 = None
    try:
        from app.data.history_loader import load_price_matrix
        close_m, *_ = load_price_matrix(today, provider, n_days=25)
        today_close = close_m.iloc[-1]
        ma5 = close_m.tail(5).mean()
        ma20 = close_m.tail(20).mean()
        valid = today_close.notna() & ma5.notna() & ma20.notna()
        breadth_ma5 = float((today_close[valid] > ma5[valid]).mean()) * 100
        breadth_ma20 = float((today_close[valid] > ma20[valid]).mean()) * 100
        parts.append(
            f"  市场广度：站上MA5占比 {breadth_ma5:.1f}% | 站上MA20占比 {breadth_ma20:.1f}%"
            f"（{'多头主导' if breadth_ma5 > 60 else '空头主导' if breadth_ma5 < 35 else '多空胶着'}）"
        )
    except Exception as e:
        logger.warning("[环境] 市场广度计算失败: %s", e)

    # —— 3. 量能 ——
    parts.append(f"  涨跌停：涨停{limit_up} / 跌停{limit_down} | 涨跌家数：{up_count}涨 / {down_count}跌 | 成交额{total_amt_yi:.0f}亿")

    # —— 3.5 连板高度（情绪强度核心指标）——
    lianban = None
    if code2name:
        try:
            lb = _lianban_stats(today, provider, code2name)
            if lb["max_height"] > 0:
                lianban = lb["max_height"]
                dist = "、".join(f"{n}板{c}只" for n, c in sorted(lb["distribution"].items(), reverse=True) if n >= 2)
                tops = "、".join(f"{nm}({code})" for nm, code, _ in lb["top_stocks"])
                emo = ("高度亢奋" if lb["max_height"] >= 6 else
                       "情绪健康" if lb["max_height"] >= 4 else
                       "情绪一般" if lb["max_height"] >= 3 else "情绪冰点/无高度")
                parts.append(
                    f"  连板高度：最高{lb['max_height']}板（{emo}）| 连板分布：{dist or '无2板以上'}"
                    + (f" | 最高板：{tops}" if tops else "")
                )
        except Exception as e:
            logger.warning("[环境] 连板高度计算失败: %s", e)

    # —— 4. 启发式阶段判断（供LLM参考，最终由LLM结合新闻定）——
    regime, confidence, risk = _classify_regime(limit_up, limit_down, breadth_ma5, breadth_ma20, index_signals)
    pos_cap = {"risk_on": 0.15, "neutral": 0.08, "risk_off": 0.03}[risk]
    parts.append(
        f"\n  📐 启发式阶段建议：{regime}（置信度{confidence:.2f}）| 风控级别={risk} | 建议单票仓位上限={pos_cap}"
    )
    parts.append("  （以上为量化启发式，请结合新闻面在报告中给出最终阶段判断）")
    return "\n".join(parts)


def _classify_regime(limit_up, limit_down, breadth_ma5, breadth_ma20, index_signals) -> tuple[str, float, str]:
    """
    基于量化指标的启发式市场阶段分类。
    返回 (阶段标签, 置信度, 风控级别)。
    """
    b5 = breadth_ma5 if breadth_ma5 is not None else 50
    # 上证回撤（负值越大越弱）
    sh_dd = index_signals.get("上证指数", 0)

    # 主升：广度强 + 涨停多 + 指数未大幅回撤
    if b5 > 65 and limit_up > 80 and sh_dd > -3:
        return "主升/普涨", 0.75, "risk_on"
    # 退潮：广度弱 + 涨停少 + 跌停多
    if b5 < 40 and limit_up < 40 and limit_down > 30:
        return "退潮", 0.70, "risk_off"
    # 反抽：指数前期回撤但广度从低位修复 + 涨停回升
    if sh_dd < -2 and b5 > 45 and limit_up > 60:
        return "退潮反抽", 0.65, "neutral"
    # 低吸：广度中性偏弱 + 跌停极少 + 缩量企稳
    if 40 <= b5 <= 60 and limit_down < 15:
        return "震荡/低吸", 0.60, "neutral"
    # 偏强震荡
    if b5 >= 55 and limit_up >= 50:
        return "偏强震荡", 0.62, "risk_on"
    return "震荡", 0.55, "neutral"


def _recent_trade_dates(provider, today: str, n: int = 4) -> list[str]:
    """返回包含 today 在内的最近 n 个交易日（升序）。"""
    start = (datetime.datetime.strptime(today, "%Y%m%d") - datetime.timedelta(days=25)).strftime("%Y%m%d")
    try:
        cal = provider.get_trade_cal(start, today)
        days = sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist())
        return days[-n:]
    except Exception as e:
        logger.warning("[盘后] 交易日历获取失败: %s", e)
        return [today]


def _sector_panorama(
    today: str,
    provider,
    df_daily: pd.DataFrame,
    df_mf: pd.DataFrame | None,
    code2ind: dict[str, str],
    code2name: dict[str, str],
) -> str:
    """
    构建板块全景：以行业为单位，整合涨幅、主力资金、3日资金趋势、领涨个股。
    输出层次清晰的"板块→个股"文本，供 LLM 生成一目了然的板块分析。
    """
    if not code2ind:
        return ""

    daily = df_daily.copy()
    daily["_ind"] = daily["ts_code"].map(code2ind)
    daily = daily.dropna(subset=["_ind"])

    # —— 行业今日表现：涨幅中位数（抗新股极端值）、上涨家数占比 ——
    grp = daily.groupby("_ind")
    ind_pct = grp["pct_chg"].median()
    ind_up_ratio = grp["pct_chg"].apply(lambda s: (s > 0).mean())
    ind_count = grp.size()

    # —— 行业今日主力资金 ——
    ind_mf_today = pd.Series(dtype=float)
    if df_mf is not None and not df_mf.empty and "net_mf_amount" in df_mf.columns:
        mf = df_mf.copy()
        mf["_ind"] = mf["ts_code"].map(code2ind)
        ind_mf_today = mf.dropna(subset=["_ind"]).groupby("_ind")["net_mf_amount"].sum() / 10000

    # —— 行业近3日主力资金累计（衡量"潜力/趋势"）——
    ind_mf_3d = pd.Series(dtype=float)
    try:
        recent_days = _recent_trade_dates(provider, today, n=3)
        frames = []
        for d in recent_days:
            dmf = provider.get_money_flow(d)
            if dmf is not None and not dmf.empty and "net_mf_amount" in dmf.columns:
                tmp = dmf[["ts_code", "net_mf_amount"]].copy()
                tmp["_ind"] = tmp["ts_code"].map(code2ind)
                frames.append(tmp.dropna(subset=["_ind"]))
        if frames:
            allmf = pd.concat(frames, ignore_index=True)
            ind_mf_3d = allmf.groupby("_ind")["net_mf_amount"].sum() / 10000
    except Exception as e:
        logger.debug("[盘后] 3日行业资金趋势计算失败: %s", e)

    # —— 汇总成行业表 ——
    board = pd.DataFrame({
        "涨幅中位": ind_pct,
        "上涨占比": ind_up_ratio,
        "成分数": ind_count,
        "今日主力": ind_mf_today,
        "三日主力": ind_mf_3d,
    }).fillna(0.0)
    # 过滤掉成分股太少的行业（统计不可靠）
    board = board[board["成分数"] >= 3]

    # Phase E：板块趋势评分（0-100）+ 阶段分类
    board["趋势评分"] = (
        50
        + board["涨幅中位"] * 4
        + (board["上涨占比"] - 0.5) * 40
        + board["今日主力"].clip(-15, 15)
        + (board["三日主力"] / 10).clip(-15, 15)
    ).clip(0, 100)

    def _stage(row) -> str:
        score, pct, mf_today, mf_3d = row["趋势评分"], row["涨幅中位"], row["今日主力"], row["三日主力"]
        if score >= 70 and pct >= 3:
            return "主升"
        if mf_3d > 5 and pct < 3:
            return "低吸潜伏"
        if score < 42 or (mf_today < 0 and mf_3d < 0):
            return "退潮"
        if 42 <= score < 55:
            return "震荡"
        return "趋势"

    board["阶段"] = board.apply(_stage, axis=1)

    def _top_stocks(ind_name: str, k: int = 3) -> str:
        """某行业内今日涨幅Top（排除新股），带名称+涨幅+资金。"""
        sub = daily[(daily["_ind"] == ind_name) & (daily["pct_chg"] <= 21)]
        sub = sub.nlargest(k, "pct_chg")
        items = []
        for _, r in sub.iterrows():
            nm = code2name.get(r["ts_code"], "")
            items.append(f"{nm}({r['ts_code'].split('.')[0]}) {r['pct_chg']:+.1f}%")
        return "、".join(items)

    lines: list[str] = ["\n========== 板块全景（行业维度） =========="]

    # 1) 主力资金流入 Top10 板块（"大量资金/主力流入"）
    top_money = board.sort_values("今日主力", ascending=False).head(10)
    lines.append("\n💰【主力资金净流入 Top10 板块】（今日｜近3日累计）")
    for ind, r in top_money.iterrows():
        trend = "🔥加速" if r["三日主力"] > r["今日主力"] * 2.5 and r["今日主力"] > 0 else (
                "📈持续" if r["三日主力"] > 0 else "⚠️背离")
        lines.append(
            f"  {ind}：今日{r['今日主力']:+.1f}亿 / 3日{r['三日主力']:+.1f}亿 {trend}"
            f" | 涨幅{r['涨幅中位']:+.1f}% | 领涨：{_top_stocks(ind)}"
        )

    # 2) 涨幅 Top10 板块（"热门板块"）
    top_gain = board.sort_values("涨幅中位", ascending=False).head(10)
    lines.append("\n🔥【涨幅 Top10 板块】（按行业涨幅中位数）")
    for ind, r in top_gain.iterrows():
        lines.append(
            f"  {ind}：涨幅{r['涨幅中位']:+.1f}% | 上涨占比{r['上涨占比']*100:.0f}%"
            f" | 主力{r['今日主力']:+.1f}亿 | 领涨：{_top_stocks(ind)}"
        )

    # 3) 潜力板块：3日资金持续流入但今日涨幅温和（未过热，"有潜力的"）
    potential = board[(board["三日主力"] > 5) & (board["涨幅中位"] < 4)].sort_values("三日主力", ascending=False).head(6)
    if not potential.empty:
        lines.append("\n🌱【潜力板块】（3日资金持续流入 + 今日涨幅温和未过热）")
        for ind, r in potential.iterrows():
            lines.append(
                f"  {ind}：3日主力{r['三日主力']:+.1f}亿 | 今日{r['今日主力']:+.1f}亿"
                f" | 涨幅仅{r['涨幅中位']:+.1f}% | 关注：{_top_stocks(ind)}"
            )

    # 4) 资金流出 Top5 板块（"需回避的退潮板块"）
    bottom_money = board.sort_values("今日主力").head(5)
    lines.append("\n❄️【主力资金流出 Top5 板块】（退潮/回避）")
    for ind, r in bottom_money.iterrows():
        lines.append(f"  {ind}：今日{r['今日主力']:+.1f}亿 / 3日{r['三日主力']:+.1f}亿 | 涨幅{r['涨幅中位']:+.1f}%")

    # 5) Phase E：板块趋势评分榜（趋势评分 + 阶段）
    top_score = board.sort_values("趋势评分", ascending=False).head(10)
    lines.append("\n📊【板块趋势评分榜 Top10】（评分0-100 | 阶段）")
    for ind, r in top_score.iterrows():
        lines.append(
            f"  {ind}：评分{r['趋势评分']:.0f} | 阶段={r['阶段']}"
            f" | 涨幅{r['涨幅中位']:+.1f}% 主力{r['今日主力']:+.1f}亿/3日{r['三日主力']:+.1f}亿"
        )

    return "\n".join(lines)


def _fetch_market_data(today: str) -> str:
    """
    拉取当日 A 股收盘实盘数据，用于盘后报告。
    返回结构化文本供 LLM 分析，包含：
      - 全市场情绪指标（板块感知的涨跌停比、涨跌家数、成交额）
      - 北向资金 + 全市场主力资金
      - 板块全景（行业涨幅/资金/3日趋势/领涨个股下钻）
      - 个股量化数据（涨幅Top10、成交额Top10、超大单净流入Top10，均附股票名称）
    """
    from app.data.composite_provider import CompositeProvider
    provider = CompositeProvider()
    sections: list[str] = []

    # 先拿 stock_basic 建立 代码→名称 / 代码→行业 映射（用于精确涨停判断+个股标名）
    code2name: dict[str, str] = {}
    code2ind: dict[str, str] = {}
    df_basic = None
    try:
        df_basic = provider.get_stock_basic()
        if df_basic is not None and not df_basic.empty:
            code2name = dict(zip(df_basic["ts_code"], df_basic["name"]))
            if "industry" in df_basic.columns:
                code2ind = dict(zip(df_basic["ts_code"], df_basic["industry"]))
    except Exception as e:
        logger.warning("[盘后] stock_basic 拉取失败: %s", e)

    def _label(ts_code: str) -> str:
        """代码 + 名称，如 300308.SZ中际旭创。"""
        nm = code2name.get(ts_code, "")
        return f"{ts_code}{nm}" if nm else ts_code

    # ── 1. 全市场日线 ──────────────────────────────────────────────────
    df_daily = None
    try:
        df_daily = provider.get_daily(today)
        if df_daily is not None and not df_daily.empty:
            df_daily = df_daily.copy()
            df_daily["pct_chg"] = pd.to_numeric(df_daily["pct_chg"], errors="coerce")
            df_daily["amount"] = pd.to_numeric(df_daily["amount"], errors="coerce")
            pct = df_daily["pct_chg"]

            limit_up, limit_down = _count_limit_moves(df_daily, code2name)
            up_count   = int((pct > 0).sum())
            down_count = int((pct < 0).sum())
            flat_count = int((pct == 0).sum())
            total_amt  = df_daily["amount"].sum() / 100000  # 千元 → 亿元
            ud_ratio   = f"{limit_up}:{limit_down}" if limit_down else f"{limit_up}:0"
            sentiment  = ("极度亢奋" if limit_up > 100 else
                          "偏多" if limit_up > 50 else
                          "中性" if limit_up > 20 else "偏空")

            sections.append(
                f"【市场情绪】{sentiment}（板块感知精确统计）\n"
                f"  上涨 {up_count} 家 | 下跌 {down_count} 家 | 平盘 {flat_count} 家\n"
                f"  涨停 {limit_up} 家 | 跌停 {limit_down} 家 | 涨跌停比 {ud_ratio}\n"
                f"  全市场成交额 {total_amt:.0f} 亿元"
            )

            # Phase A：市场环境分桶（指数趋势+广度+阶段判断）
            try:
                regime_text = _market_regime(
                    today, provider, df_daily,
                    limit_up, limit_down, up_count, down_count, total_amt,
                    code2name=code2name,
                )
                if regime_text:
                    sections.append(regime_text)
            except Exception as e:
                logger.warning("[盘后] 市场环境分桶失败: %s", e)

            # 涨幅 Top10（剔除无涨跌停限制的新股极端值，避免误导）
            df_real = df_daily[df_daily["pct_chg"] <= 31].copy()
            top10_pct = df_real.nlargest(10, "pct_chg")
            sections.append("【今日涨幅 Top10】")
            for _, r in top10_pct.iterrows():
                amt_yi = r["amount"] / 100000
                ind = code2ind.get(r["ts_code"], "")
                sections.append(f"  {_label(r['ts_code'])}  {r['pct_chg']:+.2f}%  成交{amt_yi:.1f}亿  {ind}")

            # 成交额 Top10（市场资金最集中的个股）
            top10_amt = df_daily.nlargest(10, "amount")
            sections.append("【今日成交额 Top10（资金最集中）】")
            for _, r in top10_amt.iterrows():
                amt_yi = r["amount"] / 100000
                ind = code2ind.get(r["ts_code"], "")
                sections.append(f"  {_label(r['ts_code'])}  {r['pct_chg']:+.2f}%  成交{amt_yi:.1f}亿  {ind}")
        else:
            sections.append("【全市场概况】Tushare 日线数据尚未更新（收盘后约15~30分钟入库）")
    except Exception as e:
        logger.warning("[盘后] 日线数据拉取失败: %s", e)

    # ── 2. 北向资金 ────────────────────────────────────────────────────
    try:
        df_north = provider.get_north_flow(today)
        if df_north is not None and not df_north.empty:
            north_val = float(df_north["north_money"].iloc[0])
            direction = "净流入🟢" if north_val >= 0 else "净流出🔴"
            sections.append(f"【北向资金】{north_val / 10000:+.1f} 亿元 {direction}")
        else:
            sections.append("【北向资金】数据尚未更新")
    except Exception as e:
        logger.warning("[盘后] 北向资金拉取失败: %s", e)

    # ── 3. 主力资金 + 行业分布 ─────────────────────────────────────────
    try:
        df_mf = provider.get_money_flow(today)

        if df_mf is not None and not df_mf.empty and "net_mf_amount" in df_mf.columns:
            df_mf = df_mf.copy()
            total_net = df_mf["net_mf_amount"].sum() / 10000  # 万元 → 亿元
            direction = "净流入🟢" if total_net >= 0 else "净流出🔴"
            sections.append(f"【全市场主力资金】{total_net:+.1f} 亿元 {direction}")

            # 超大单净流入 Top10 个股（附名称）
            if "buy_elg_amount" in df_mf.columns and "sell_elg_amount" in df_mf.columns:
                df_mf["elg_net"] = (
                    pd.to_numeric(df_mf["buy_elg_amount"], errors="coerce") -
                    pd.to_numeric(df_mf["sell_elg_amount"], errors="coerce")
                ) / 10000
                top10_elg = df_mf.nlargest(10, "elg_net")
                sections.append("【超大单净流入 Top10（主力真实意图）】")
                for _, r in top10_elg.iterrows():
                    net = r["net_mf_amount"] / 10000
                    ind = code2ind.get(r["ts_code"], "")
                    sections.append(
                        f"  {_label(r['ts_code'])}  超大单净流入{r['elg_net']:+.1f}亿  主力净{net:+.1f}亿  {ind}"
                    )

        else:
            sections.append("【主力资金】数据尚未更新（晚间18:00后入库）")
    except Exception as e:
        logger.warning("[盘后] 主力资金拉取失败: %s", e)
        df_mf = None

    # ── 4. 板块全景（行业维度：涨幅/资金/3日趋势/领涨股下钻）──────────────
    try:
        if df_daily is not None and not df_daily.empty and code2ind:
            panorama = _sector_panorama(today, provider, df_daily, df_mf, code2ind, code2name)
            if panorama:
                sections.append(panorama)
    except Exception as e:
        logger.warning("[盘后] 板块全景构建失败: %s", e)

    # ── 5. 个股量化画像（Phase C+D：换手/量比/千股千评/拥挤度）────────────
    try:
        if df_daily is not None and not df_daily.empty:
            profile = _enrich_candidates(today, provider, df_daily, df_mf, code2name, code2ind)
            if profile:
                sections.append(profile)
    except Exception as e:
        logger.warning("[盘后] 个股量化画像构建失败: %s", e)

    return "\n".join(sections) if sections else "（实盘数据暂未获取）"


def _generate_analysis(
    session: SessionType,
    label: str,
    now_str: str,
    news_df: pd.DataFrame,
    today: str,
    *,
    notices_text: str = "",
    research_text: str = "",
) -> str:
    """调用 DeepSeek 生成时段分析。三个时段使用不同数据源和分析视角。"""
    from app.llm.client import LLMClient
    llm = LLMClient()

    # 整理新闻文本（带来源+时间标签）
    if news_df.empty:
        news_text = ""
        news_count = 0
    else:
        title_col = "标题" if "标题" in news_df.columns else news_df.columns[0]
        time_col = "发布时间" if "发布时间" in news_df.columns else news_df.columns[1]
        lines = []
        for _, row in news_df.head(80).iterrows():
            t = str(row.get(time_col, ""))[:16]
            raw = str(row.get(title_col, ""))
            title = raw.split("】")[-1].strip() if "】" in raw else raw
            source = str(row.get("来源", "新闻"))
            lines.append(f"[{source}][{t}] {title[:150]}")
        news_text = "\n".join(lines)
        news_count = len(news_df)

    # 盘后额外拉取 A 股实盘数据
    market_data_text = ""
    if session == "post":
        market_data_text = _fetch_market_data(today)

    prompt = _build_prompt(
        session, label, now_str, news_text, news_count, today,
        notices_text=notices_text,
        research_text=research_text,
        market_data_text=market_data_text,
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是专业A股策略分析师。\n"
                "【铁律】只分析用户提供的信息，严禁编造或引用未出现的内容。\n"
                "来源标注规则（必须执行）：每条结论末尾标注来源和时间，"
                "格式：[财联社 13:36] / [华尔街见闻 14:06] / [公告] / [行情数据]。\n"
                "分析必须落地到具体A股板块和代表性个股（附6位股票代码）。"
            ),
        },
        {"role": "user", "content": prompt},
    ]

    return llm.chat(messages, task_type="pro", max_tokens=3500)


def _build_prompt(
    session: SessionType,
    label: str,
    now_str: str,
    news_text: str,
    news_count: int,
    today: str,
    *,
    notices_text: str = "",
    research_text: str = "",
    market_data_text: str = "",
) -> str:
    """三个时段使用完全不同的数据源组合和分析视角。"""

    date_str = f"{today[:4]}-{today[4:6]}-{today[6:]}"

    # ------------------------------------------------------------------ #
    # 盘前：只用昨日15:00→今日09:00的新闻，聚焦隔夜信息+开盘预判
    # ------------------------------------------------------------------ #
    if session == "pre":
        info = f"""当前时间：{now_str}，A股交易日：{date_str}

【隔夜新闻（昨日15:00 → 今日09:00，共{news_count}条，按时间倒序）】
{news_text if news_text else "（无隔夜新闻）"}
"""
        if notices_text:
            info += f"\n【今日盘前重大公告】\n{notices_text}\n"

        return info + """
请生成【盘前快讯】，只分析上方提供的隔夜信息，Markdown格式：

## 🌅 盘前快讯 · """ + now_str + """

> 💡 **今日一句话摘要：**（在此写一句话，如"美伊协议落地，油价暴跌，航运/黄金高开，算力延续强势"，20字内，供标题使用）

### 一、隔夜核心消息（影响今日开盘的前3-5条）
每条格式：📌 **[来源 时间] 消息摘要** → A股影响板块/个股（写股票代码） → 开盘预判方向

（只写与A股开盘直接相关的，纯海外无关消息忽略）

### 二、今日开盘板块预判
| 板块 | 开盘方向 | 核心逻辑（来源+时间） | 代表个股（代码） |
|---|---|---|---|
（列4-6个板块，方向：🟢高开/🔴低开/⚪平开，逻辑必须对应上方新闻）

### 三、开盘重点盯盘个股（2-3只）
每只格式：**股票名称(代码)** — 关注理由 — 关键价位（突破/支撑） — 来源

### 四、今日开盘风险提示
（1-2条，必须来自上方新闻，说明哪种情况下不要追高）

### 五、今日重要时间节点
（今日盘中有无重要数据/政策发布，几点发布，影响哪个板块）
"""

    # ------------------------------------------------------------------ #
    # 盘中：上午09:00→当前时刻的新闻，聚焦已发生的盘面 + 午后策略
    # ------------------------------------------------------------------ #
    elif session == "mid":
        info = f"""当前时间：{now_str}，A股交易日：{date_str}

【上午盘中新闻（09:00 → 当前，共{news_count}条，按时间倒序）】
{news_text if news_text else "（无盘中新闻）"}
"""
        if notices_text:
            info += f"\n【今日重大公告】\n{notices_text}\n"

        return info + """
请生成【盘中快讯】，基于上方已发生的信息，Markdown格式：

## ☀️ 盘中快讯 · """ + now_str + """

> 💡 **今日一句话摘要：**（在此写一句话，如"算力半导体领涨，原油暴跌利好航运，午后关注存储芯片接力"，20字内，供标题使用）

### 一、上午盘面催化（已验证的板块异动）
每条格式：📌 **[来源 时间] 事件** → 板块方向（🟢涨/🔴跌） → 代表个股涨跌表现预判

（只写上方新闻中有明确板块联动的，列3-5条）

### 二、上午热点板块
| 板块 | 方向 | 催化逻辑（来源+时间） | 值得关注个股（代码） |
|---|---|---|---|

### 三、午后操作策略
**🟢 午后可关注：** 列具体个股+理由+参考价位
**🔴 午后需回避：** 列具体个股+回避理由（消息出尽/过热/风险）
**⚪ 等待信号：** 哪些逻辑还需验证才能介入

### 四、下午重要时间节点
（今日下午有无数据公布/讲话/事件，几点，影响哪个板块）
"""

    # ------------------------------------------------------------------ #
    # 盘后：A股实盘数据（板块资金流+涨跌统计）为主，新闻为辅，量化选股
    # ------------------------------------------------------------------ #
    else:  # post
        info = f"""当前时间：{now_str}，A股交易日：{date_str}

【A股今日实盘数据】
{market_data_text if market_data_text else "（数据获取失败）"}

【今日全天新闻（09:00→收盘，共{news_count}条，按时间倒序）】
{news_text if news_text else "（无新闻）"}
"""
        if notices_text:
            info += f"\n【今日重大公告】\n{notices_text}\n"
        if research_text:
            info += f"\n【观察池近期券商研报】\n{research_text}\n"

        return info + """
请生成【盘后复盘报告】，核心是把上方实盘数据组织成「板块→题材→个股」一目了然的结构。

【分析铁律】
1. 必须以实盘数据为准，新闻仅作催化解释；**数据与新闻矛盾时以资金/行情为准**。
2. 每个板块/个股结论必须"双证据"：既要有资金/趋势数据，又要有新闻/题材逻辑，缺一不可。
3. 若某板块新闻利好但资金3日净流出或趋势背离，必须标注"⚠️新闻与资金背离→以资金为准→回避"。
4. 直接采用上方【市场环境】的阶段判断和风控级别，据此决定推荐积极度（risk_off时只观察不买入）。

Markdown格式：

## 🌙 盘后复盘 · """ + now_str + """

> 💡 **今日一句话摘要：**（一句话，如"证券银行领涨·算力链资金大幅流出·情绪极度亢奋"，25字内，供标题用）

### 一、市场环境定调（顶层）
基于上方【市场环境】数据，给出：
- **市场阶段**：（退潮/退潮反抽/震荡低吸/偏强震荡/主升普涨，参考启发式建议并结合新闻最终判断）
- **置信度**：X.XX
- **风控级别**：risk_on / neutral / risk_off
- **建议单票仓位上限**：X.XX
- **一句话定调**：（指数趋势+广度+量能，2句内）

### 二、市场温度计
| 指标 | 数值 | 评分 | 解读 |
|---|---|---|---|
| 涨跌停比 | 填实际值 | /20 | >5:1强势 |
| 上涨家数占比 | 填% | /20 | >60%偏多 |
| 站上MA5占比 | 填% | /20 | >60%多头主导 |
| 成交额 | 填亿 | /20 | >2.5万亿活跃 |
| 主力资金/北向 | 填亿 | /20 | 流入为正 |

**综合 XX/100 → 市场状态：强势/偏多/中性/偏弱/弱势**（资金数据缺失则标注"待入库"不计分）

### 三、🔥 今日热门板块（资金+涨幅，按重要性排序）
直接基于上方【板块全景】数据，列5-6个最值得关注的板块：

| 板块 | 今日涨幅 | 主力资金(今日/3日) | 趋势 | 领涨个股 | 催化 |
|---|---|---|---|---|---|
（趋势用🔥加速/📈持续/⚠️背离，领涨个股带名称代码，催化对应新闻来源+时间；新闻与资金背离必须标注⚠️）

### 四、🌱 潜力板块（资金潜伏、尚未充分表现）
基于【潜力板块】数据，列2-3个"3日资金持续流入但今日涨幅温和"的板块，说明潜伏逻辑。

### 五、💰 主力重点个股（超大单净流入Top）
直接用【超大单净流入Top10】数据，选信号最强的5只：

| 个股(代码) | 所属板块 | 超大单净流入 | 主力净 | 涨跌幅 | 解读 |
|---|---|---|---|---|---|
（解读：是板块龙头/独立逻辑/异动，1句话）

### 六、🎯 明日重点关注（精选3只，量化+消息双验证）
**选股铁律**：
1. 必须从上方【个股量化画像】中挑选，优先拥挤度=✅健康/⚠️偏热、且超大单净流入为正；
2. 🔥过热只能"观察"不可"买入"（追高风险）；
3. **带🚨负面标记（立案/减持/问询/退市等）的个股一律排除，不得推荐**；
4. risk_off 环境下全部只能"观察"。
每只：
**①名称(代码)** — 所属热门板块 | 拥挤度标记
- 📊 量化：涨幅X% | 换手X% | 量比X | 超大单净流入X亿 | 千评得分X | 机构参与X% | 人气排名X
- 🎯 逻辑：（必须同时有"资金证据"+"消息/题材证据"，缺一不可）
- 💰 参考买入：X-X元（保守=回踩5日线/VWAP支撑 / 激进=今日收盘附近）
- 🌅 次日开盘确认（9:30-9:40）：（具体3条，如"开盘在X元上方不破昨日均价、量能维持昨日80%以上、所属板块未集体低开"，不满足则放弃）
- 🛑 止损：X元（跌破支撑离场）
- 🎯 止盈：+5%减仓一半，+8%继续减仓（趋势走弱分批退出）
- ⚠️ 失效条件：个股级 + 板块级（如"板块指数跌2%或资金转净流出"）
- 🏷️ 建议：**买入** / **观察**

### 七、❄️ 明日需回避（基于资金流出/背离/过热/负面事件）
列2-3个板块或个股，必须给量化理由（如"半导体3日净流出168亿仍在退潮"、"XX股被立案调查"）。
若上方有🚨负面事件个股出现在热门榜，必须在此点名提示。

### 八、明日大盘研判
- 市场阶段延续判断（结合一、市场环境 + **连板高度**：高度<3情绪弱、≥4健康、≥6亢奋）
- 量能预判：萎缩/持平/放量
- 指数方向：强势/震荡/谨慎
- 一句话核心逻辑（基于今日资金主线 + 板块轮动方向 + 连板梯队强度）
"""


def _headline(content: str) -> str:
    """
    从报告正文提取标题摘要。
    优先抓 > 💡 **今日一句话摘要：** 后的内容，
    次选第一条 📌 催化事件，最后兜底取首行正文。
    """
    import re

    def _clean(text: str) -> str:
        text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"\[[^\]]*\]", "", text)
        text = re.sub(r"[#>`~_]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    for line in content.split("\n"):
        line = line.strip()
        # 优先：专门的摘要行
        if "今日一句话摘要" in line:
            part = re.split(r"[：:]", line, maxsplit=1)[-1].strip()
            part = part.lstrip("* \t")   # 去掉 ** 残留
            cleaned = _clean(part)
            if cleaned and len(cleaned) > 3:
                return cleaned[:32]

    for line in content.split("\n"):
        line = line.strip()
        # 次选：📌 催化事件
        if line.startswith("📌"):
            cleaned = _clean(line.lstrip("📌").strip())
            part = cleaned.split("→")[0].strip()
            return part[:30] if part else cleaned[:30]

    # 兜底
    for line in content.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith(">") and len(line) > 8:
            return _clean(line)[:30]

    return "A股快讯"


def _window_desc(start_h: int, end_h: int) -> str:
    """生成时间窗口描述，如 '昨日15:00 → 今日09:00'。end_h==-1 表示动态当前时刻。"""
    if start_h < 0:
        return f"昨日{24 + start_h:02d}:00 → 今日{end_h:02d}:00"
    if end_h == -1:
        return f"今日{start_h:02d}:00 → 当前时刻"
    return f"今日{start_h:02d}:00 → {end_h:02d}:00"


def _offset_date(date_str: str, days: int) -> str:
    dt = datetime.datetime.strptime(date_str, "%Y%m%d") + datetime.timedelta(days=days)
    return dt.strftime("%Y%m%d")
