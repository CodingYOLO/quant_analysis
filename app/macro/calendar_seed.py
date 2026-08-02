"""事件日历种子（L4）。三类事件·三种诚实度，绝不混淆：

① 规则性事件（由公开规则推算·带"以官方为准"标注）：LPR 每月20日、中国月度数据的
   惯例发布旬、美国非农=每月第一个周五、财报法定披露截止日、政治局会议月份规律。
② 已核实事件（**逐日期人工核实·note 记来源与核实日**）：FOMC 决议、美国 CPI。
   核实于 2026-08-02：FOMC 对照 federalreserve.gov 官网日历；未核实的绝不写具体日期。
③ 数据推算事件（由库内/接口数据算出）：大额解禁日（未来90天·当日解禁市值≥300亿）。

手工录入（is_manual=1）永不被种子覆盖——upsert 以 (event_date,event_type,title) 为键，
种子事件的键与手工事件天然不同。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.macro import store

logger = logging.getLogger(__name__)

# ── ② 已核实事件（写死具体日期必须带核实记录·过期后由未来会话续期） ────────────
_VERIFIED_US: list[dict] = [
    # FOMC 决议日=会议第二天(北京时间次日凌晨02:00公布)。核实于2026-08-02·federalreserve.gov
    # 官网日历（官网注明"每个日期在前一次会议确认前为暂定"）。
    {"event_date": "20260916", "event_type": "fomc", "title": "FOMC 利率决议(9/15-16会议)",
     "importance": 3, "region": "US",
     "note": "北京时间9/17凌晨02:00·含经济预测(SEP)·核实于2026-08-02 federalreserve.gov"},
    {"event_date": "20261028", "event_type": "fomc", "title": "FOMC 利率决议(10/27-28会议)",
     "importance": 3, "region": "US",
     "note": "北京时间10/29凌晨·核实于2026-08-02 federalreserve.gov"},
    {"event_date": "20261209", "event_type": "fomc", "title": "FOMC 利率决议(12/8-9会议)",
     "importance": 3, "region": "US",
     "note": "北京时间12/10凌晨·含SEP·核实于2026-08-02 federalreserve.gov"},
    # 美国 CPI：8:30 ET(北京时间当晚20:30/21:30)。核实于2026-08-02·usinflationcalculator
    # 发布日程表(转载BLS官方日程·bls.gov直连403)。
    {"event_date": "20260812", "event_type": "us_cpi", "title": "美国CPI(7月值)", "importance": 3,
     "region": "US", "note": "北京时间当晚20:30·核实于2026-08-02"},
    {"event_date": "20260911", "event_type": "us_cpi", "title": "美国CPI(8月值)", "importance": 3,
     "region": "US", "note": "北京时间当晚20:30·核实于2026-08-02"},
    {"event_date": "20261014", "event_type": "us_cpi", "title": "美国CPI(9月值)", "importance": 3,
     "region": "US", "note": "核实于2026-08-02"},
    {"event_date": "20261110", "event_type": "us_cpi", "title": "美国CPI(10月值)", "importance": 3,
     "region": "US", "note": "核实于2026-08-02"},
    {"event_date": "20261210", "event_type": "us_cpi", "title": "美国CPI(11月值)", "importance": 3,
     "region": "US", "note": "核实于2026-08-02"},
]


def rule_events(start: str, months: int = 3) -> list[dict]:
    """规则性事件（推算·非精确日期的一律在 title/note 里写明"预计/以官方为准"）。"""
    out: list[dict] = []
    t0 = pd.Timestamp(start)
    for k in range(months + 1):
        m = (t0 + pd.DateOffset(months=k)).replace(day=1)
        ym = m.strftime("%Y%m")
        out.append({"event_date": f"{ym}20", "event_type": "lpr", "title": "LPR 报价",
                    "importance": 2, "region": "CN", "note": "每月20日·遇节假日顺延"})
        out.append({"event_date": f"{ym}09", "event_type": "cn_cpi", "title": "中国CPI/PPI(预计上旬)",
                    "importance": 2, "region": "CN", "note": "统计局·惯例9日前后·以官方日程为准"})
        out.append({"event_date": f"{ym}13", "event_type": "cn_sf", "title": "社融/M1/M2(预计中旬)",
                    "importance": 2, "region": "CN", "note": "央行·10-15日不定期·以官方为准"})
        month_end = (m + pd.offsets.MonthEnd(0)).strftime("%Y%m%d")
        out.append({"event_date": month_end, "event_type": "cn_pmi", "title": "制造业PMI",
                    "importance": 2, "region": "CN", "note": "统计局·当月最后一天发布"})
        # 美国非农：每月第一个周五（规则事件·偶有顺延）
        d = m
        while d.weekday() != 4:
            d += pd.Timedelta(days=1)
        out.append({"event_date": d.strftime("%Y%m%d"), "event_type": "us_nfp",
                    "title": "美国非农就业", "importance": 3, "region": "US",
                    "note": "规则=每月第一个周五·北京时间当晚20:30·偶有顺延"})
        # 政治局会议月份规律（4/7/12月·仅月份可预期·具体日期以新华社通稿为准）
        if m.month in (4, 7, 12):
            out.append({"event_date": f"{ym}25", "event_type": "meeting",
                        "title": f"{m.month}月中央政治局会议(预计)", "importance": 3, "region": "CN",
                        "note": "仅月份有规律(通常下旬)·此日期为占位·以官方公告为准"})
        if m.month == 12:
            out.append({"event_date": f"{ym}15", "event_type": "meeting",
                        "title": "中央经济工作会议(预计)", "importance": 3, "region": "CN",
                        "note": "通常12月中旬·此日期为占位·以官方公告为准"})
    # 财报法定披露截止（交易所规则·确定性日期）
    for dl, title in ((f"{t0.year}0430", "年报+一季报披露截止"), (f"{t0.year}0831", "中报披露截止"),
                      (f"{t0.year}1031", "三季报披露截止"),
                      (f"{t0.year + 1}0430", "年报+一季报披露截止")):
        if start <= dl <= (t0 + pd.DateOffset(months=months + 1)).strftime("%Y%m%d"):
            out.append({"event_date": dl, "event_type": "earnings", "title": title,
                        "importance": 2, "region": "CN", "note": "交易所法定截止日·临近日集中披露"})
    return out


def unlock_events(start: str, horizon_days: int = 90, min_yi: float = 300.0) -> list[dict]:
    """大额解禁日：未来 horizon 天内·单日解禁市值 ≥ min_yi 亿。

    市值 = 最新收盘价 × float_share(**股**·非文档标注的万股·见 ts_flow 交叉验证)。
    价格用当前最新价——未来解禁只能按现价估·note 已注明"按现价估算"。
    """
    from app.data.cache import rate_limited_call
    from app.data.composite_provider import CompositeProvider
    from app.macro.adapters.base import paged_fetch
    from app.macro.adapters.ts_rates import _pro
    prov = CompositeProvider()
    try:
        latest = store.latest_date()
        daily = prov.get_daily(latest)
        close = dict(zip(daily["ts_code"], pd.to_numeric(daily["close"], errors="coerce")))
        name_map = dict(zip(daily["ts_code"], [""] * len(daily)))
        try:
            sb = prov.get_stock_basic()
            name_map = dict(zip(sb["ts_code"], sb["name"]))
        except Exception:
            pass
    except Exception as e:
        logger.warning("[macro] 解禁日历：取行情失败·跳过 %s", e)
        return []

    frames, cur = [], pd.Timestamp(start)
    hi = cur + pd.Timedelta(days=horizon_days)
    while cur <= hi:                                       # 15天片(offset上限见 ts_flow)
        seg = min(cur + pd.Timedelta(days=14), hi)
        try:
            chunk = paged_fetch(
                lambda off, a=cur, b=seg: rate_limited_call(
                    "tushare_share_float", _pro().share_float,
                    start_date=a.strftime("%Y%m%d"), end_date=b.strftime("%Y%m%d"),
                    limit=6000, offset=off), page=6000)
            if not chunk.empty:
                frames.append(chunk)
        except Exception as e:
            logger.warning("[macro] 解禁日历片 %s~%s 失败: %s", cur.date(), seg.date(), e)
        cur = seg + pd.Timedelta(days=1)
    if not frames:
        return []
    ev = pd.concat(frames, ignore_index=True)
    keep = [c for c in ("ts_code", "float_date", "holder_name", "float_share") if c in ev.columns]
    ev = ev.drop_duplicates(subset=keep)
    ev["mv_yi"] = [float(close.get(t) or 0) * float(s or 0) / 1e8
                   for t, s in zip(ev["ts_code"], ev["float_share"])]
    out = []
    for d, g in ev.groupby(ev["float_date"].astype(str).str.replace("-", "")):
        tot = g["mv_yi"].sum()
        if tot < min_yi:
            continue
        top = g.sort_values("mv_yi", ascending=False).head(3)
        names = "、".join(name_map.get(t, t) or t for t in top["ts_code"])
        out.append({"event_date": d, "event_type": "float_release",
                    "title": f"大额解禁≈{tot:.0f}亿", "importance": 2, "region": "CN",
                    "note": f"按现价估算·前三:{names}"})
    return out


def seed(months: int = 3) -> int:
    """幂等播种（每晚随 macro-sync 刷新·手工行不受影响）。"""
    start = pd.Timestamp.now().strftime("%Y%m%d")
    rows = rule_events(start, months) + list(_VERIFIED_US)
    try:
        rows += unlock_events(start)
    except Exception as e:
        logger.warning("[macro] 解禁日历生成失败(其余照常): %s", e)
    n = store.upsert_calendar(rows)
    logger.info("[macro] 事件日历播种 %d 条", n)
    return n
