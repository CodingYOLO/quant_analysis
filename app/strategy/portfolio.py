"""
💼 持仓闭环：自选盯盘 + 持仓盈亏 + 持仓体检 + 事件预警。

把全站的"发现"能力接到"实盘持有"上：用户钉住的票（自选/持仓）→ 一处聚合看
现价/盈亏/技术位置/资金/赛道，给红黄绿灯体检结论，并把【跌破止损/破位/解禁/减持/大宗折价】
这些"埋着"的风险**主动推到面前**。

数据走 CompositeProvider；现价用实时报价、技术用日线面板、资金用主力净流入、事件复用 fundamentals。
诚实红线：资金为主力估算、研报观点≠事实、不预测涨跌、不构成投资建议。
"""

from __future__ import annotations

import datetime
import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.strategy import db

logger = logging.getLogger(__name__)

# 体检/预警阈值（集中可调）
_FLOW_OUT_YI = -0.3        # 主力近3日净流出超此值(亿)记一条
_PNL_WARN = -10.0         # 持仓浮亏超此(%)预警
_FLOAT_NEAR_DAYS = 30     # 解禁临近天数
_FLOAT_BIG_RATIO = 1.0    # 解禁比例(%)≥此视为值得提示
_BLOCK_DISCOUNT = 2.0     # 大宗折价(%)≥此视为出货
_DISCLAIMER = ("持仓体检/预警基于真实行情与公开事件；资金为主力估算、不预测涨跌、不构成投资建议。"
               "止损/仓位请自行决策。")


# ──────────────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────────────

def build_portfolio(provider: CompositeProvider | None = None) -> dict:
    """读取自选/持仓 → 逐只体检(现价/盈亏/技术/资金/事件/赛道/健康灯) + 汇总预警。"""
    provider = provider or CompositeProvider()
    watch = db.get_watchlist()
    if not watch:
        return {"ok": True, "rows": [], "alerts": [], "summary": {"n": 0, "n_holding": 0},
                "disclaimer": _DISCLAIMER, "msg": "还没有自选/持仓——在上方添加，或在选股池/牛股发掘/因子选股里点「+自选」"}

    codes = [w["ts_code"] for w in watch]
    date = _latest_trade_date(provider)
    quotes = _quote_map(provider, codes)
    tech = _tech_map(provider, codes, date)
    flows = _flow_map(provider, date)
    industries = _industry_map(provider)
    sectors = _sector_phase_map(date, provider)

    rows = []
    for w in watch:
        rows.append(_build_row(w, quotes, tech, flows, industries, sectors, provider))

    alerts = _collect_alerts(rows)
    n_hold = sum(1 for r in rows if r["is_holding"])
    return {"ok": True, "date": date, "rows": rows, "alerts": alerts,
            "summary": {"n": len(rows), "n_holding": n_hold,
                        "total_pnl": _total_pnl(rows)},
            "disclaimer": _DISCLAIMER}


def _build_row(w: dict, quotes: dict, tech: dict, flows: dict,
               industries: dict, sectors: dict, provider: CompositeProvider) -> dict:
    """单只体检行。"""
    ts = w["ts_code"]
    q = quotes.get(ts, {})
    tk = tech.get(ts, {})
    price = q.get("price") or tk.get("close")
    cost = w.get("cost")
    pnl = round((price / cost - 1) * 100, 2) if (price and cost) else None
    events = _alert_events(ts, provider)
    ind = industries.get(ts, "")
    sec = sectors.get(ind) or {}

    row = {
        "ts_code": ts, "name": w.get("name") or "", "is_holding": bool(w["is_holding"]),
        "cost": cost, "shares": w.get("shares"), "stop_loss": w.get("stop_loss"),
        "note": w.get("note") or "",
        "price": round(price, 2) if price else None,
        "pct_chg": q.get("pct_chg"),
        "pnl": pnl,
        "industry": ind,
        "sector_phase": sec.get("phase", ""), "sector_rps": sec.get("avg_rps"),
        "main_flow_3d": flows.get(ts),
        "ma20": tk.get("ma20"), "ma60": tk.get("ma60"),
        "above_ma20": tk.get("above_ma20"), "above_ma60": tk.get("above_ma60"),
        "bias20": tk.get("bias20"), "dist_high": tk.get("dist_high"),
        "events": events,
    }
    level, flags = _health(row)
    row["health"], row["flags"] = level, flags
    return row


# ──────────────────────────────────────────────────────────────────────────
# 体检健康灯 + 预警（纯函数，可单测）
# ──────────────────────────────────────────────────────────────────────────

def _health(r: dict) -> tuple[str, list[dict]]:
    """
    红黄绿灯 + 触发项。规则透明：
      🔴 警惕：跌破止损 / (破位MA20 且 (主力净流出 或 有事件雷))
      🟢 健康：无任何触发项
      🟡 留意：其余(破位/流出/浮亏/解禁/减持/大宗 任一)
    每个触发项 {text, level(warn/danger)}。
    """
    flags: list[dict] = []
    price, stop = r.get("price"), r.get("stop_loss")
    if stop and price and price <= stop:
        flags.append({"text": f"跌破止损位 {stop}", "level": "danger"})
    if r.get("above_ma20") is False:
        flags.append({"text": "跌破MA20(破位)", "level": "warn"})
    flow = r.get("main_flow_3d")
    if flow is not None and flow < _FLOW_OUT_YI:
        flags.append({"text": f"主力3日净流出 {flow:+.2f}亿", "level": "warn"})
    if r.get("is_holding") and r.get("pnl") is not None and r["pnl"] <= _PNL_WARN:
        flags.append({"text": f"浮亏 {r['pnl']:+.1f}%", "level": "warn"})
    sector_weak = "破位" in (r.get("sector_phase") or "")          # 所在板块整体弱势破位·防板块退潮
    if sector_weak:
        flags.append({"text": f"所在板块【{r.get('industry', '')}】弱势破位·当心板块退潮", "level": "warn"})

    ev = r.get("events") or {}
    fl = ev.get("float")
    if fl and fl.get("next_days") is not None and fl["next_days"] <= _FLOAT_NEAR_DAYS \
            and (fl.get("next_ratio") or 0) >= _FLOAT_BIG_RATIO:
        flags.append({"text": f"{fl['next_days']}天后解禁 {fl['next_ratio']}%", "level": "warn"})
    ht = ev.get("holder_trade")
    if ht and (ht.get("de_count") or 0) > 0:
        flags.append({"text": f"近期股东减持 {ht['de_count']}次", "level": "warn"})
    bl = ev.get("block")
    if bl and bl.get("premium_avg") is not None and bl["premium_avg"] <= -_BLOCK_DISCOUNT:
        flags.append({"text": f"大宗折价 {bl['premium_avg']:.1f}%(出货)", "level": "warn"})

    if not flags:
        return "green", flags
    danger = any(f["level"] == "danger" for f in flags)
    broke = r.get("above_ma20") is False
    bad_fundamental = ((flow is not None and flow < _FLOW_OUT_YI)
                       or bool(ev.get("float") or ev.get("holder_trade") or ev.get("block"))
                       or sector_weak)            # 破位MA20 且 板块也破位 → 升级红灯
    if danger or (broke and bad_fundamental):
        return "red", flags
    return "yellow", flags


def _collect_alerts(rows: list[dict]) -> list[dict]:
    """汇总需要主动提示的预警（红灯全收 + 黄灯里的止损/解禁/减持/大宗），按严重度排序。"""
    out = []
    for r in rows:
        for f in r.get("flags", []):
            out.append({"ts_code": r["ts_code"], "name": r["name"],
                        "level": f["level"], "text": f["text"],
                        "is_holding": r["is_holding"]})
    order = {"danger": 0, "warn": 1}
    out.sort(key=lambda a: (order.get(a["level"], 9), not a["is_holding"]))
    return out


def _total_pnl(rows: list[dict]) -> float | None:
    """持仓总浮盈%（按市值加权·需成本+数量；缺数据则按等权持仓盈亏均值兜底）。"""
    weighted, mv = 0.0, 0.0
    simple = []
    for r in rows:
        if not r["is_holding"] or r.get("pnl") is None:
            continue
        simple.append(r["pnl"])
        if r.get("cost") and r.get("shares"):
            cost_mv = r["cost"] * r["shares"]
            weighted += cost_mv * r["pnl"] / 100
            mv += cost_mv
    if mv > 0:
        return round(weighted / mv * 100, 2)
    return round(sum(simple) / len(simple), 2) if simple else None


# ──────────────────────────────────────────────────────────────────────────
# 数据采集（批量优先，避开逐只重复取数）
# ──────────────────────────────────────────────────────────────────────────

def _latest_trade_date(provider: CompositeProvider) -> str:
    """最近有日线数据的交易日（往回找 10 天）。"""
    today = datetime.date.today()
    for i in range(10):
        d = (today - datetime.timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = provider.get_daily(d)
        except Exception:
            df = None
        if df is not None and not df.empty:
            return d
    return today.strftime("%Y%m%d")


def _quote_map(provider: CompositeProvider, codes: list[str]) -> dict:
    """实时报价（现价+当日涨跌·一次批量）。失败返回空。"""
    try:
        df = provider.get_realtime_quote(codes)
    except Exception:
        logger.debug("[持仓] 实时报价失败")
        return {}
    if df is None or df.empty:
        return {}
    return {str(r["ts_code"]): {"price": float(r["price"]), "pct_chg": float(r.get("pct_chg") or 0)}
            for _, r in df.iterrows()}


def _tech_map(provider: CompositeProvider, codes: list[str], date: str) -> dict:
    """技术位置（MA20/60·乖离·距120日高·一次全市场面板提取自选列）。"""
    from app.data.history_loader import load_price_matrix
    try:
        close_m, *_ = load_price_matrix(date, provider, n_days=130)
    except Exception:
        logger.debug("[持仓] 面板加载失败")
        return {}
    if close_m is None or close_m.empty:
        return {}
    out = {}
    for ts in codes:
        if ts not in close_m.columns:
            continue
        s = close_m[ts].dropna()
        if len(s) < 25:
            continue
        cur = float(s.iloc[-1])
        ma20 = float(s.tail(20).mean())
        ma60 = float(s.tail(60).mean()) if len(s) >= 60 else None
        high120 = float(s.tail(120).max())
        out[ts] = {
            "close": round(cur, 2), "ma20": round(ma20, 2),
            "ma60": round(ma60, 2) if ma60 else None,
            "above_ma20": cur >= ma20, "above_ma60": (cur >= ma60) if ma60 else None,
            "bias20": round((cur - ma20) / ma20 * 100, 2) if ma20 else None,
            "dist_high": round((cur / high120 - 1) * 100, 2) if high120 else None,
        }
    return out


def _flow_map(provider: CompositeProvider, date: str) -> dict:
    """主力近3日净流入（亿）·复用 signals._main_flow_3d 全市场口径，提取所需。"""
    try:
        from app.strategy.signals import _main_flow_3d
        return {k: round(v, 2) for k, v in _main_flow_3d(provider, date).items()}
    except Exception:
        logger.debug("[持仓] 主力资金加载失败")
        return {}


def _industry_map(provider: CompositeProvider) -> dict:
    """ts_code → 申万二级行业。"""
    try:
        sb = provider.get_stock_basic()
        return dict(zip(sb["ts_code"], sb["industry"].fillna("")))
    except Exception:
        return {}


def _sector_phase_map(date: str, provider: CompositeProvider) -> dict:
    """行业 → 板块强弱(phase/avg_rps)。读【最新一份已缓存】的因子表(板块强弱是慢变盘后信号)，
    无任何缓存则返回空——避免在持仓页触发因子表重建(~30-60秒)拖慢加载；用过选股页后即有缓存。"""
    try:
        from app.config import get_settings
        from app.strategy.screener import _FACTOR_TABLE_VERSION
        files = sorted((get_settings().cache_dir / "factor_table")
                       .glob(f"*_{_FACTOR_TABLE_VERSION}.parquet"))
        if not files:
            return {}
        latest = files[-1].name.split("_")[0]          # 最新缓存因子表的交易日
        from app.strategy.sector_strength import build_sector_strength
        res = build_sector_strength(latest, provider)
        return {s["industry"]: s for s in res.get("sectors", [])} if res.get("ok") else {}
    except Exception:
        return {}


def _alert_events(ts_code: str, provider: CompositeProvider) -> dict:
    """预警相关事件（解禁/减持/大宗折价）·复用 fundamentals 汇总helper·best-effort。"""
    from app.strategy.fundamentals import (
        _block_trade_summary, _float_summary, _holder_trade_summary, _safe_fetch,
    )
    out: dict = {}
    try:
        fl = _float_summary(_safe_fetch(provider, "get_share_float", ts_code))
        if fl:
            out["float"] = fl
        ht = _holder_trade_summary(_safe_fetch(provider, "get_holder_trade", ts_code))
        if ht:
            out["holder_trade"] = ht
        bl = _block_trade_summary(ts_code, provider)
        if bl:
            out["block"] = bl
    except Exception:
        logger.debug("[持仓] 事件加载失败 %s", ts_code)
    return out
