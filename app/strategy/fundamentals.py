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
    return {
        "ok": True, "ts_code": ts_code,
        "fields": [{"key": k, "label": lbl} for k, lbl in _FINA_FIELDS],
        "rows": rows,                          # 新→旧
        "summary": _fina_summary(rows),
        "latest_period": latest["period"],
        "forecast": _latest_forecast(ts_code, provider),
    }


# 业绩预告类型 → 多空倾向（红=利好/绿=利空，对标A股涨红跌绿）
_FORECAST_GOOD = ("预增", "略增", "续盈", "扭亏", "减亏")          # 减亏=亏损收窄(向好)
_FORECAST_BAD = ("预减", "略减", "首亏", "续亏", "预亏", "增亏")   # 增亏=亏损扩大(向坏)


def _latest_forecast(ts_code: str, provider: CompositeProvider) -> dict | None:
    """最新一期业绩预告（前瞻信号：预增/预亏/扭亏 + 净利变动幅度）。"""
    try:
        df = provider.get_forecast(ts_code)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.copy()
    df["ann_date"] = df["ann_date"].astype(str)
    r = df.sort_values("ann_date", ascending=False).iloc[0]
    ftype = str(r.get("type") or "")
    pmin = pd.to_numeric(r.get("p_change_min"), errors="coerce")
    pmax = pd.to_numeric(r.get("p_change_max"), errors="coerce")
    rng = None
    if pd.notna(pmin) or pd.notna(pmax):
        lo = f"{float(pmin):+.0f}" if pd.notna(pmin) else "?"
        hi = f"{float(pmax):+.0f}" if pd.notna(pmax) else "?"
        rng = f"{lo}~{hi}%" if lo != hi else f"{lo}%"
    level = "bad" if ftype in _FORECAST_BAD else ("good" if ftype in _FORECAST_GOOD else "neutral")
    return {
        "type": ftype, "period": _fmt_period(str(r.get("end_date") or "")),
        "ann_date": _fmt_date(r["ann_date"]), "net_change": rng,
        "summary": str(r.get("summary") or "")[:60], "level": level,
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
