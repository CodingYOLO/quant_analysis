"""
基本面 + 消息面速览（与技术面「股性速览」互补）：
  - 财报跟踪：ROE / 营收同比 / 净利同比 / 资产负债率 / 毛利率 近几期趋势（Tushare fina_indicator）。
  - LLM 近期提示：博查联网搜索真实新闻 → v4-flash 接地总结「近期该注意什么」（按日缓存）。

信息源：Tushare 财务指标（权威）+ 博查 Web Search（权威源、反编造）。
LLM 严格接地：只基于材料、标来源、不预测涨跌、不给买卖建议。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)


# ── 财报跟踪 ────────────────────────────────────────────────────────────────
_FINA_FIELDS = [
    ("roe", "ROE(累计)%"), ("netprofit_yoy", "净利同比%"), ("or_yoy", "营收同比%"),
    ("debt_to_assets", "资产负债率%"), ("grossprofit_margin", "毛利率%"),
]


def get_financials(ts_code: str, provider: CompositeProvider | None = None,
                   periods: int = 6) -> dict:
    """近 periods 个报告期的关键财务指标 + 趋势 + 健康提示。"""
    provider = provider or CompositeProvider()
    try:
        df = provider.get_fina_indicator(ts_code)
    except Exception as e:
        return {"ok": False, "msg": f"财务数据获取失败：{e}"}
    if df is None or df.empty:
        return {"ok": False, "msg": "暂无财务数据"}

    df = df.copy()
    df["end_date"] = df["end_date"].astype(str)
    # 同一报告期可能有原始/重述多行 → 每期保留首行（Tushare 按公告日新→旧，首行=最新口径）
    df = df.drop_duplicates(subset="end_date", keep="first")
    df = df.sort_values("end_date", ascending=False).head(periods)
    if df.empty:
        return {"ok": False, "msg": "暂无财务数据"}

    rows = []
    for _, r in df.iterrows():
        item = {"period": _fmt_period(r["end_date"])}
        for col, _label in _FINA_FIELDS:
            v = pd.to_numeric(r.get(col), errors="coerce")
            item[col] = round(float(v), 2) if pd.notna(v) else None
        rows.append(item)

    latest = rows[0]
    latest_actual_end = str(df.iloc[0]["end_date"])     # 最新实际财报报告期(YYYYMMDD)·用于剔除过期预告
    return {
        "ok": True, "ts_code": ts_code,
        "fields": [{"key": k, "label": lbl} for k, lbl in _FINA_FIELDS],
        "rows": rows,                          # 新→旧
        "summary": _fina_summary(rows),
        "latest_period": latest["period"],
        "forecast": _latest_forecast(ts_code, provider, latest_actual_end),
        "survey": _survey_summary(ts_code, provider),   # 机构调研热度（关注度信号）
        "events": _events_summary(ts_code, provider),   # 事件/避雷面：解禁/增减持/快报/户数
    }


# ── 事件/避雷面（解禁 / 增减持 / 业绩快报 / 股东户数）──────────────────────────

def _safe_fetch(provider: CompositeProvider, method: str, ts_code: str):
    """取数 best-effort：失败返回 None，避免单接口异常拖垮整个基本面。"""
    try:
        return getattr(provider, method)(ts_code)
    except Exception:
        return None


def _events_summary(ts_code: str, provider: CompositeProvider) -> dict | None:
    """汇总事件面：解禁(抛压)/增减持(利空利好)/业绩快报(前瞻)/户数(筹码集中)。全空返回 None。"""
    out: dict = {}
    fl = _float_summary(_safe_fetch(provider, "get_share_float", ts_code))
    if fl:
        out["float"] = fl
    ht = _holder_trade_summary(_safe_fetch(provider, "get_holder_trade", ts_code))
    if ht:
        out["holder_trade"] = ht
    ex = _express_summary(_safe_fetch(provider, "get_express", ts_code))
    if ex:
        out["express"] = ex
    hn = _holdernum_summary(_safe_fetch(provider, "get_holder_number", ts_code))
    if hn:
        out["holdernum"] = hn
    bl = _block_trade_summary(ts_code, provider)
    if bl:
        out["block"] = bl
    mg = _margin_summary(_safe_fetch(provider, "get_margin_detail", ts_code))
    if mg:
        out["margin"] = mg
    rp = _repurchase_summary(_safe_fetch(provider, "get_repurchase", ts_code))
    if rp:
        out["repurchase"] = rp
    return out or None


def _margin_summary(df) -> dict | None:
    """个股两融：最新融资余额(亿) + 近~5日变化%（增=杠杆加仓·减=撤离/踩踏风险）。"""
    if df is None or df.empty or "rzye" not in df.columns:
        return None
    df = df.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values("trade_date")
    rz = pd.to_numeric(df["rzye"], errors="coerce").dropna().tolist()
    if not rz:
        return None
    chg, trend = None, ""
    if len(rz) >= 6 and rz[-6] > 0:
        chg = round((rz[-1] - rz[-6]) / rz[-6] * 100, 2)
        trend = "杠杆资金加仓" if chg > 0 else "杠杆资金撤离"
    return {"rzye_yi": round(rz[-1] / 1e8, 2), "chg_pct": chg, "trend": trend,
            "date": _fmt_date(str(df["trade_date"].iloc[-1]))}


def _repurchase_summary(df) -> dict | None:
    """股份回购：近~13个月最新一条 进度(完成/实施中/预案) + 金额(亿)。预案=可能喊话，实施中/完成才实。"""
    import datetime
    if df is None or df.empty or "ann_date" not in df.columns:
        return None
    df = df.copy()
    df["ann_date"] = df["ann_date"].astype(str)
    cutoff = (datetime.date.today() - datetime.timedelta(days=400)).strftime("%Y%m%d")
    df = df[df["ann_date"] >= cutoff]
    if df.empty:
        return None
    r = df.sort_values("ann_date", ascending=False).iloc[0]
    amt = pd.to_numeric(r.get("amount"), errors="coerce")
    proc = str(r.get("proc") or "")
    return {"proc": proc, "ann_date": _fmt_date(str(r.get("ann_date") or "")),
            "amount_yi": round(float(amt) / 1e8, 2) if pd.notna(amt) else None,
            "is_real": proc in ("完成", "实施中", "股东大会通过")}


def _block_trade_summary(ts_code: str, provider: CompositeProvider) -> dict | None:
    """取大宗交易 + 当日收盘 → 折溢价（关键信号），best-effort。"""
    bt = _safe_fetch(provider, "get_block_trade", ts_code)
    if bt is None or bt.empty:
        return None
    import datetime
    t = datetime.date.today()
    try:
        daily = provider.get_stock_daily(
            ts_code, (t - datetime.timedelta(days=185)).strftime("%Y%m%d"), t.strftime("%Y%m%d"))
    except Exception:
        daily = None
    return _block_trade_calc(bt, daily)


def _block_trade_calc(bt_df, daily_df) -> dict | None:
    """大宗交易：近180天 笔数/总额(亿)/平均折溢价(折价=抛压·溢价=接盘)/机构接盘笔数。"""
    if bt_df is None or bt_df.empty or "trade_date" not in bt_df.columns:
        return None
    bt = bt_df.copy()
    bt["trade_date"] = bt["trade_date"].astype(str)
    close_map = {}
    if daily_df is not None and not daily_df.empty and "close" in daily_df.columns:
        close_map = dict(zip(daily_df["trade_date"].astype(str),
                             pd.to_numeric(daily_df["close"], errors="coerce")))
    price = pd.to_numeric(bt.get("price"), errors="coerce")
    close = bt["trade_date"].map(close_map)
    prem = (price - close) / close * 100
    amt = round(float(pd.to_numeric(bt.get("amount"), errors="coerce").fillna(0).sum()) / 1e4, 2)  # 万元→亿
    prem_avg = round(float(prem.dropna().mean()), 2) if prem.notna().any() else None
    inst = int(bt.get("buyer", pd.Series(dtype=str)).astype(str).str.contains("机构专用").sum())
    latest = bt.sort_values("trade_date", ascending=False).iloc[0]
    return {"count": int(len(bt)), "amount_yi": amt, "premium_avg": prem_avg,
            "inst_buy": inst, "latest_date": _fmt_date(str(latest["trade_date"]))}


def _float_summary(df) -> dict | None:
    """限售解禁：聚合到解禁日，给出下一次解禁(日期/比例/距今天数) + 未来解禁场次。"""
    import datetime
    if df is None or df.empty or "float_date" not in df.columns:
        return None
    df = df.copy()
    df["float_date"] = df["float_date"].astype(str)
    df["_fr"] = pd.to_numeric(df.get("float_ratio"), errors="coerce").fillna(0.0)
    today = datetime.date.today().strftime("%Y%m%d")
    by_date = df.groupby("float_date")["_fr"].sum().sort_index()
    upcoming = by_date[by_date.index >= today]
    if upcoming.empty:
        return None
    d = upcoming.index[0]
    days = (datetime.datetime.strptime(d, "%Y%m%d").date() - datetime.date.today()).days
    return {"next_date": _fmt_date(d), "next_ratio": round(float(upcoming.iloc[0]), 4),
            "next_days": int(days), "upcoming_count": int(len(upcoming))}


def _holder_trade_summary(df) -> dict | None:
    """股东增减持：近180天 减持/增持 次数 + 最近一条（谁/增减/比例）。"""
    if df is None or df.empty or "in_de" not in df.columns:
        return None
    df = df.copy()
    df["ann_date"] = df["ann_date"].astype(str)
    de = int((df["in_de"] == "DE").sum())
    inn = int((df["in_de"] == "IN").sum())
    r = df.sort_values("ann_date", ascending=False).iloc[0]
    ratio = pd.to_numeric(r.get("change_ratio"), errors="coerce")
    return {"de_count": de, "in_count": inn,
            "latest": {"date": _fmt_date(str(r.get("ann_date") or "")),
                       "holder": str(r.get("holder_name") or "")[:16],
                       "type": "减持" if r.get("in_de") == "DE" else "增持",
                       "ratio": round(float(ratio), 2) if pd.notna(ratio) else None}}


def _express_summary(df) -> dict | None:
    """业绩快报：最新一期 净利同比/营收(亿)/ROE（比业绩预告更接近真实）。"""
    if df is None or df.empty:
        return None
    df = df.copy()
    df["ann_date"] = df["ann_date"].astype(str)
    r = df.sort_values("ann_date", ascending=False).iloc[0]

    def _num(k):
        v = pd.to_numeric(r.get(k), errors="coerce")
        return round(float(v), 2) if pd.notna(v) else None

    rev = _num("revenue")
    ni = _num("n_income")             # 本期净利润
    prev = _num("yoy_net_profit")     # ⚠️Tushare express 此字段是「去年同期净利润」，非百分比
    yoy = round((ni - prev) / abs(prev) * 100, 1) if (ni is not None and prev) else None
    return {"period": _fmt_period(str(r.get("end_date") or "")),
            "ann_date": _fmt_date(str(r.get("ann_date") or "")),
            "net_profit_yoy": yoy,                                     # 真实净利同比%
            "net_profit_yi": round(ni / 1e8, 2) if ni else None,
            "revenue_yi": round(rev / 1e8, 2) if rev else None,
            "roe": _num("diluted_roe")}


def _holdernum_summary(df) -> dict | None:
    """股东户数：最新户数 + 环比（户数减少=筹码集中，向好）。"""
    if df is None or df.empty or "holder_num" not in df.columns:
        return None
    df = df.copy()
    df["end_date"] = df["end_date"].astype(str)
    df = df.sort_values("end_date", ascending=False).drop_duplicates("end_date")
    nums = pd.to_numeric(df["holder_num"], errors="coerce").dropna().tolist()
    if not nums:
        return None
    latest = int(nums[0])
    chg, trend = None, ""
    if len(nums) >= 2 and nums[1] > 0:
        chg = round((nums[0] - nums[1]) / nums[1] * 100, 1)
        trend = "户数减少·筹码集中" if chg < 0 else "户数增加·筹码分散"
    return {"latest": latest, "chg_pct": chg, "trend": trend,
            "date": _fmt_date(str(df["end_date"].iloc[0]))}


def _survey_summary(ts_code: str, provider: CompositeProvider) -> dict | None:
    """近一年机构调研：近90/180天次数（关注度热度）+ 最近3条（日期/形式）。"""
    try:
        df = provider.get_survey(ts_code)
    except Exception:
        return None
    if df is None or df.empty or "surv_date" not in df.columns:
        return None
    import datetime
    df = df.copy()
    df["surv_date"] = df["surv_date"].astype(str)
    today = datetime.date.today()
    cut90 = (today - datetime.timedelta(days=90)).strftime("%Y%m%d")
    cut180 = (today - datetime.timedelta(days=180)).strftime("%Y%m%d")
    c90 = int((df["surv_date"] >= cut90).sum())
    c180 = int((df["surv_date"] >= cut180).sum())
    df = df.sort_values("surv_date", ascending=False)
    recent = [{"date": _fmt_date(str(r["surv_date"])), "mode": str(r.get("rece_mode") or "")[:24]}
              for _, r in df.head(3).iterrows()]
    heat = "高" if c90 >= 5 else ("中" if c90 >= 2 else "低")
    return {"count_90d": c90, "count_180d": c180, "heat": heat, "recent": recent}


def get_analyst_rc(ts_code: str, provider: CompositeProvider | None = None) -> dict:
    """
    券商盈利预测/目标价（report_rc）。按需调用（5100档限频1次/小时，日缓存兜底）。
    返回 {ok, target_avg/low/high, upside_hint(需前端按现价算), n_reports, n_org, ratings, latest}。
    """
    provider = provider or CompositeProvider()
    try:
        df = provider.get_report_rc(ts_code)
    except Exception as e:
        return {"ok": False, "msg": f"盈利预测接口限频，请稍后重试：{str(e)[:50]}"}
    if df is None or df.empty:
        return {"ok": False, "msg": "近半年无券商盈利预测覆盖"}
    return _analyst_summary(df)


def _analyst_summary(df: pd.DataFrame) -> dict:
    """report_rc 明细 → 目标价区间 + 一致评级分布 + 覆盖机构数（纯函数，便于单测）。"""
    mx = pd.to_numeric(df.get("max_price"), errors="coerce")
    mn = pd.to_numeric(df.get("min_price"), errors="coerce")
    mid = pd.concat([mx, mn], axis=1).mean(axis=1)
    ratings: dict[str, int] = {}
    for v in df.get("rating", pd.Series(dtype=str)).dropna():
        s = str(v).strip()
        if s:
            ratings[s] = ratings.get(s, 0) + 1
    dcol = next((c for c in ("report_date", "create_time") if c in df.columns), None)
    latest = _fmt_date(str(df[dcol].astype(str).max())[:8]) if dcol and len(df) else ""
    return {
        "ok": True,
        "target_avg": round(float(mid.mean()), 2) if mid.notna().any() else None,
        "target_low": round(float(mn.min()), 2) if mn.notna().any() else None,
        "target_high": round(float(mx.max()), 2) if mx.notna().any() else None,
        "n_reports": int(len(df)),
        "n_org": int(df["org_name"].nunique()) if "org_name" in df.columns else 0,
        "ratings": dict(sorted(ratings.items(), key=lambda kv: kv[1], reverse=True)),
        "latest": latest,
    }


# ── 东方财富研报（免费·不限频·替代 Tushare report_rc）──────────────────────────
def get_em_research(ts_code: str, provider: CompositeProvider | None = None,
                    recent_days: int = 180) -> dict:
    """
    东财个股研报汇总：近期机构覆盖/评级分布/盈利预测增速(成长性)/近1月研报数/PDF原文链接。
    比 Tushare report_rc 更优（免费、不限频、含 PDF + 多年盈利预测）。
    """
    provider = provider or CompositeProvider()
    try:
        df = provider.get_research_report_em(ts_code)
    except Exception as e:
        return {"ok": False, "msg": f"东财研报获取失败：{str(e)[:50]}"}
    if df is None or df.empty:
        return {"ok": False, "msg": "近期无券商研报覆盖"}
    return _em_research_summary(df, recent_days)


def get_ths_forecast(ts_code: str, provider: CompositeProvider | None = None) -> dict:
    """同花顺一致预期：覆盖机构数 + EPS 一致预期(均值/区间) + 隐含增速 + 行业平均(相对成长性)。"""
    provider = provider or CompositeProvider()
    try:
        df = provider.get_profit_forecast_ths(ts_code)
    except Exception as e:
        return {"ok": False, "msg": f"同花顺一致预期获取失败：{str(e)[:50]}"}
    if df is None or df.empty:
        return {"ok": False, "msg": "同花顺暂无一致预期"}
    return _ths_forecast_summary(df)


def _ths_forecast_summary(df: pd.DataFrame) -> dict:
    """同花顺盈利预测明细(分年度:机构数/最小/均值/最大/行业平均) → 一致预期摘要。纯函数，便于单测。"""
    rows = []
    for r in df.to_dict("records"):           # 同花顺源也是 pyarrow dtype，用 records 迭代更稳
        y = str(r.get("年度", "")).strip()
        avg = pd.to_numeric(r.get("均值"), errors="coerce")
        if not y or pd.isna(avg):
            continue
        rows.append({
            "year": y,
            "n_org": (int(pd.to_numeric(r.get("预测机构数"), errors="coerce"))
                      if pd.notna(pd.to_numeric(r.get("预测机构数"), errors="coerce")) else 0),
            "eps_avg": round(float(avg), 2),
            "eps_min": round(float(pd.to_numeric(r.get("最小值"), errors="coerce")), 2) if pd.notna(pd.to_numeric(r.get("最小值"), errors="coerce")) else None,
            "eps_max": round(float(pd.to_numeric(r.get("最大值"), errors="coerce")), 2) if pd.notna(pd.to_numeric(r.get("最大值"), errors="coerce")) else None,
            "ind_avg": round(float(pd.to_numeric(r.get("行业平均数"), errors="coerce")), 2) if pd.notna(pd.to_numeric(r.get("行业平均数"), errors="coerce")) else None,
        })
    rows.sort(key=lambda x: x["year"])
    if not rows:
        return {"ok": False, "msg": "同花顺一致预期为空"}
    growth = None
    if len(rows) >= 2 and rows[0]["eps_avg"]:
        growth = round((rows[1]["eps_avg"] / rows[0]["eps_avg"] - 1) * 100, 1)
    return {"ok": True, "by_year": rows, "max_n_org": max(r["n_org"] for r in rows),
            "eps_growth": growth, "ind_avg": rows[0].get("ind_avg")}


def _em_research_summary(df: pd.DataFrame, recent_days: int = 180) -> dict:
    """东财研报明细 → 评级分布/买入占比/盈利预测增速/近1月数/最新5条(含PDF)。纯函数，便于单测。"""
    import datetime
    import re
    df = df.copy()
    df["日期"] = df["日期"].astype(str)
    today = datetime.date.today()
    cutoff = (today - datetime.timedelta(days=recent_days)).strftime("%Y-%m-%d")
    recent = df[df["日期"] >= cutoff]
    if recent.empty:                                    # 半年无覆盖 → 兜底取最新 20 条
        recent = df.sort_values("日期", ascending=False).head(20)

    ratings: dict[str, int] = {}
    for v in recent["东财评级"].dropna():
        s = str(v).strip()
        if s:
            ratings[s] = ratings.get(s, 0) + 1
    n = len(recent)
    buy = sum(c for k, c in ratings.items() if any(x in k for x in ("买入", "增持", "推荐", "强烈")))

    # 盈利预测：动态找年份列，算 consensus EPS（中位数）与隐含同比增速（成长性=真材实料）
    yr_cols = sorted(set(re.findall(r"(\d{4})-盈利预测-收益", " ".join(df.columns))))
    eps_by_year: dict[str, float] = {}
    for y in yr_cols:
        vals = pd.to_numeric(recent.get(f"{y}-盈利预测-收益"), errors="coerce").dropna()
        if len(vals):
            eps_by_year[y] = round(float(vals.median()), 2)
    eps_growth = None
    if len(eps_by_year) >= 2:
        ys = sorted(eps_by_year)
        e1, e2 = eps_by_year[ys[0]], eps_by_year[ys[1]]
        if e1 and e1 != 0:
            eps_growth = round((e2 / e1 - 1) * 100, 1)

    cut30 = (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    last_month = int((df["日期"] >= cut30).sum())
    recent_sorted = recent.sort_values("日期", ascending=False)
    items = [{"org": str(r.get("机构", "")), "rating": str(r.get("东财评级", "")),
              "title": str(r.get("报告名称", ""))[:42], "date": str(r.get("日期", "")),
              "pdf": str(r.get("报告PDF链接", "") or "")} for _, r in recent_sorted.head(5).iterrows()]
    return {
        "ok": True, "n_reports": n,
        "n_org": int(recent["机构"].nunique()) if "机构" in recent.columns else 0,
        "ratings": dict(sorted(ratings.items(), key=lambda kv: kv[1], reverse=True)),
        "buy_ratio": round(buy / n * 100) if n else 0,
        "eps_by_year": eps_by_year, "eps_growth": eps_growth,
        "last_month": last_month, "latest": (items[0]["date"] if items else ""),
        "industry": str(recent.iloc[0].get("行业", "")) if n else "",
        "recent": items,
    }


# 业绩预告类型 → 多空倾向（红=利好/绿=利空，对标A股涨红跌绿）
_FORECAST_GOOD = ("预增", "略增", "续盈", "扭亏", "减亏")          # 减亏=亏损收窄(向好)
_FORECAST_BAD = ("预减", "略减", "首亏", "续亏", "预亏", "增亏")   # 增亏=亏损扩大(向坏)


def _forecast_is_live(end_date: str, latest_actual_end: str, today: str) -> bool:
    """业绩预告是否仍「前瞻有效」——目标报告期的真实财报尚未出来。

    业绩预告只在实际定期报告披露前是前瞻信号；一旦该期实际财报已出，预告即过期，
    再展示会误导（用户曾把一年前的中报预告误读成"当前业绩暴雷"）。
    - 有实际财报基准：目标期(end_date)必须严格晚于最新实际报告期，否则已被覆盖。
    - 无基准兜底：目标期不早于今天前 ~150 天（再早则实际报告多半已出）。
    """
    if not end_date:
        return False
    if latest_actual_end:
        return end_date > latest_actual_end
    import datetime
    floor = (datetime.datetime.strptime(today, "%Y%m%d") - datetime.timedelta(days=150)).strftime("%Y%m%d")
    return end_date >= floor


def _latest_forecast(ts_code: str, provider: CompositeProvider,
                     latest_actual_end: str = "") -> dict | None:
    """最新一期**仍前瞻有效**的业绩预告（预增/预亏/扭亏 + 净利变动幅度 + 出处）。

    Args:
        latest_actual_end: 最新实际财报报告期(YYYYMMDD)，用于剔除已被实际数据覆盖的过期预告。
    """
    try:
        df = provider.get_forecast(ts_code)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    import datetime
    today = datetime.date.today().strftime("%Y%m%d")
    df = df.copy()
    df["ann_date"] = df["ann_date"].astype(str)
    df["end_date"] = df["end_date"].astype(str)
    # 关键：只保留「目标期实际财报尚未出」的预告（前瞻有效），剔除已被实际数据覆盖的旧预告
    df = df[df["end_date"].map(lambda e: _forecast_is_live(e, latest_actual_end, today))]
    if df.empty:
        return None
    r = df.sort_values(["end_date", "ann_date"], ascending=False).iloc[0]   # 取目标期最新的那条
    ftype = str(r.get("type") or "")
    pmin = pd.to_numeric(r.get("p_change_min"), errors="coerce")
    pmax = pd.to_numeric(r.get("p_change_max"), errors="coerce")
    rng = None
    if pd.notna(pmin) or pd.notna(pmax):
        lo = f"{float(pmin):+.0f}" if pd.notna(pmin) else "?"
        hi = f"{float(pmax):+.0f}" if pd.notna(pmax) else "?"
        rng = f"{lo}~{hi}%" if lo != hi else f"{lo}%"
    level = "bad" if ftype in _FORECAST_BAD else ("good" if ftype in _FORECAST_GOOD else "neutral")
    code6 = ts_code.split(".")[0]
    return {
        "type": ftype, "period": _fmt_period(str(r.get("end_date") or "")),
        "ann_date": _fmt_date(r["ann_date"]), "net_change": rng,
        "summary": str(r.get("summary") or "")[:60], "level": level,
        "source": "Tushare业绩预告(forecast·交易所披露口径)",
        "verify_url": f"https://data.eastmoney.com/notices/stock/{code6}.html",
    }


def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d


def _fmt_period(end_date: str) -> str:
    """20251231 → 2025年报；20250930→2025三季报；0630→中报；0331→一季报。"""
    y, md = end_date[:4], end_date[4:]
    return {"1231": f"{y}年报", "0930": f"{y}三季报",
            "0630": f"{y}中报", "0331": f"{y}一季报"}.get(md, f"{y}-{md}")


def _fina_summary(rows: list[dict]) -> str:
    """基于数据的客观健康提示（不预测、不荐股）。"""
    if not rows:
        return ""
    latest = rows[0]
    parts = []
    np_yoy = latest.get("netprofit_yoy")
    if np_yoy is not None:
        parts.append(f"最新期净利同比 {np_yoy:+.1f}%（{'高增长' if np_yoy >= 30 else ('增长' if np_yoy > 0 else '下滑')}）")
    or_yoy = latest.get("or_yoy")
    if or_yoy is not None:
        parts.append(f"营收同比 {or_yoy:+.1f}%")
    debt = latest.get("debt_to_assets")
    if debt is not None:
        parts.append(f"资产负债率 {debt:.0f}%（{'偏高·留意' if debt >= 60 else '健康'}）")
    gm = latest.get("grossprofit_margin")
    if gm is not None:
        parts.append(f"毛利率 {gm:.0f}%")
    return "；".join(parts)


# ── LLM 近期提示（博查新闻 + v4-flash 接地总结，按日缓存）────────────────────
def _alert_cache_path(ts_code: str, date: str) -> Path:
    p = get_settings().cache_dir / "stock_alert"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{ts_code}_{date}.json"


def get_recent_alert(ts_code: str, name: str = "",
                     provider: CompositeProvider | None = None,
                     force: bool = False) -> dict:
    """博查搜该股近一月真实新闻 → v4-flash 总结近期注意事项。按日缓存避免重复花费。"""
    import datetime
    today = datetime.date.today().strftime("%Y%m%d")
    cache = _alert_cache_path(ts_code, today)
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    result = _build_alert(ts_code, name)
    if result.get("ok"):                 # 仅缓存成功结果，避免博查偶发空结果污染当日缓存
        try:
            cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return result


# 行情快照/低质内容关键词与低质来源（剔除，保证信息源高质量）
_NOISE_KW = ("走势图", "最新价格", "行情", "诊股", "股吧", "资金流向", "盘口",
             "实时行情", "今日股价", "历史数据", "千股千评", "个股评级")
_NOISE_SITE = ("牛炒股", "股票频道", "诊股")


def _is_quality(r: dict) -> bool:
    """剔除行情快照页/低质源，保留实质性新闻（公告/调研/业绩/订单等）。"""
    import re
    title = str(r.get("title", ""))
    site = str(r.get("site", ""))
    if any(k in title for k in _NOISE_KW):
        return False
    if any(s in site for s in _NOISE_SITE):
        return False
    # 行情快照标题形如「沪电股份:133.36 8.83% +10.82 002463 搜狐证券」
    if re.search(r"[:：]\s*\d+\.\d+", title) or re.search(r"\d+\.\d+\s*%.*证券", title):
        return False
    return True


def _build_alert(ts_code: str, name: str) -> dict:
    from app.data.web_search import BochaSearchClient
    from app.llm.client import LLMClient

    bocha = BochaSearchClient()
    if not getattr(bocha, "enabled", True):
        return {"ok": False, "msg": "未配置博查 API Key，无法联网检索新闻"}

    code6 = ts_code.split(".")[0]
    query = f"{name} 公告 业绩 机构调研 订单中标 扩产 减持 股权激励 风险提示"
    try:
        res = bocha.search(query, count=12, freshness="oneMonth")
    except Exception as e:
        return {"ok": False, "msg": f"联网检索失败：{e}"}
    res = [r for r in (res or []) if (r.get("summary") or r.get("snippet")) and _is_quality(r)]
    if not res:
        return {"ok": False, "msg": "近一月未检索到该股有效新闻"}

    sources = [{"site": r.get("site", ""), "date": str(r.get("date", ""))[:10],
                "title": r.get("title", ""), "url": r.get("url", "")} for r in res[:8]]
    material = "\n".join(
        f"[{s['site']} {s['date']}] {s['title']} — {(r.get('summary') or r.get('snippet') or '')[:160]}"
        for s, r in zip(sources, res[:8]))

    prompt = (
        f"你是严谨的A股研究员。基于以下真实联网搜索材料，总结【{name}({code6})】近期值得注意的事项。\n"
        "要求：3-5 个要点，每点一句话并标注来源(媒体+日期)；只基于材料、不编造、不预测涨跌、不给买卖建议；\n"
        "覆盖业绩/订单/扩产/机构调研/减持/政策/风险等维度，材料未提及的方面写「材料未提及」。\n\n"
        f"材料：\n{material}"
    )
    try:
        summary = LLMClient().chat([{"role": "user", "content": prompt}],
                                   task_type="flash", max_tokens=2000, temperature=0.3)
    except Exception as e:
        return {"ok": False, "msg": f"LLM 总结失败：{e}"}
    if not (summary or "").strip():
        return {"ok": False, "msg": "LLM 未返回有效总结"}

    return {"ok": True, "ts_code": ts_code, "summary": summary.strip(), "sources": sources}
