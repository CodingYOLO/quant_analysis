"""
大盘情绪仪表盘数据聚合。

产出：
  - KPI（最新交易日）：情绪温度、成交额(+环比)、连板高度(+分布)、涨跌比(+涨跌家数)
  - 时间序列（近 N 个交易日）：
      成交额趋势 / 5日线占比趋势(全市场+大中小盘) / 涨跌停分布 / 连板梯队 / 指数涨跌幅

设计：
  - 所有数据走 CompositeProvider
  - 广度用 load_price_matrix 一次性算（高效）；日聚合按交易日缓存
  - 结果按 end_date 缓存到 data_cache/sentiment/{end}_{days}.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix
from app.nodes.quick_report import _board_limit_pct, _recent_trade_dates

logger = logging.getLogger(__name__)

# 展示用指数（Tushare index_daily 代码）
_INDICES = [
    ("上证指数", "000001.SH"),
    ("深证成指", "399001.SZ"),
    ("创业板指", "399006.SZ"),
    ("沪深300", "399300.SZ"),
    ("中证500", "000905.SH"),
    ("中证1000", "000852.SH"),
]

# 流通市值分层（亿元）
_CAP_MICRO = 50      # < 50亿 微盘
_CAP_LARGE = 300     # > 300亿 大盘；中间为中盘


def _cache_path(end_date: str, days: int) -> Path:
    settings = get_settings()
    p = settings.cache_dir / "sentiment"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{end_date}_{days}.json"


def build_dashboard(end_date: str, days: int = 22, force: bool = False) -> dict:
    """构建大盘情绪仪表盘数据。结果按 (end_date, days) 缓存。"""
    path = _cache_path(end_date, days)
    if path.exists() and not force:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    provider = CompositeProvider()
    # 取 days + 6 个交易日（多6天用于连板回溯），升序
    all_dates = _recent_trade_dates(provider, end_date, n=days + 6)
    if not all_dates:
        raise ValueError(f"{end_date} 无交易日数据")

    # —— 逐日聚合：成交额/涨跌家数/涨跌停集合 ——
    per_day = {}
    limit_sets: dict[str, set] = {}
    code2name = _code2name(provider)
    for d in all_dates:
        dd = provider.get_daily(d)
        if dd is None or dd.empty:
            continue
        pct = pd.to_numeric(dd["pct_chg"], errors="coerce")
        amt = pd.to_numeric(dd["amount"], errors="coerce")
        up_set, down_set = set(), set()
        for ts, p in zip(dd["ts_code"], pct):
            if pd.isna(p):
                continue
            lim = _board_limit_pct(ts, code2name.get(ts, ""))
            if lim - 0.3 <= p <= lim + 0.5:
                up_set.add(ts)
            elif -(lim + 0.5) <= p <= -(lim - 0.3):
                down_set.add(ts)
        limit_sets[d] = up_set
        per_day[d] = {
            "amount_yi": float(amt.sum() / 100000),
            "up": int((pct > 0).sum()),
            "down": int((pct < 0).sum()),
            "limit_up": len(up_set),
            "limit_down": len(down_set),
        }

    range_dates = [d for d in all_dates if d in per_day][-days:]

    # —— 连板梯队（每日 2板/3板/4板/5板+ 家数 + 最高板）——
    lianban_series = _lianban_series(all_dates, limit_sets, range_dates)

    # —— 市场广度（5日线占比，全市场+大中小盘）——
    breadth = _breadth_series(provider, end_date, range_dates)

    # —— 指数涨跌幅 ——
    indices = _index_series(provider, end_date, range_dates)

    # —— KPI（最新交易日）——
    last = range_dates[-1]
    prev = range_dates[-2] if len(range_dates) >= 2 else last
    kpi = _build_kpi(per_day, lianban_series, breadth, last, prev)

    result = {
        "end_date": end_date,
        "kpi": kpi,
        "dates": range_dates,
        "amount": [round(per_day[d]["amount_yi"] / 10000, 3) for d in range_dates],  # 万亿
        "limit_up": [per_day[d]["limit_up"] for d in range_dates],
        "limit_down": [per_day[d]["limit_down"] for d in range_dates],
        "lianban": lianban_series,
        "breadth": breadth,
        "indices": indices,
    }
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def _code2name(provider) -> dict[str, str]:
    try:
        sb = provider.get_stock_basic()
        return dict(zip(sb["ts_code"], sb["name"]))
    except Exception:
        return {}


def _lianban_series(all_dates: list[str], limit_sets: dict[str, set],
                    range_dates: list[str]) -> dict:
    """每个交易日的连板梯队：2板/3板/4板/5板+家数 + 最高板。"""
    idx = {d: i for i, d in enumerate(all_dates)}
    b3, b4, b5p, heights = [], [], [], []
    for d in range_dates:
        i = idx[d]
        today_up = limit_sets.get(d, set())
        hcount = {}
        for ts in today_up:
            h = 0
            j = i
            while j >= 0 and ts in limit_sets.get(all_dates[j], set()):
                h += 1
                j -= 1
            hcount[h] = hcount.get(h, 0) + 1
        b3.append(hcount.get(3, 0))
        b4.append(hcount.get(4, 0))
        b5p.append(sum(c for hh, c in hcount.items() if hh >= 5))
        heights.append(max(hcount.keys()) if hcount else 0)
    return {"b3": b3, "b4": b4, "b5p": b5p, "height": heights}


def _breadth_series(provider, end_date: str, range_dates: list[str]) -> dict:
    """5日线占比时间序列（全市场 + 大/中/小盘），用一次价格矩阵高效计算。"""
    try:
        close_m, *_ = load_price_matrix(end_date, provider, n_days=len(range_dates) + 8)
    except Exception as e:
        logger.warning("[情绪] 广度矩阵加载失败: %s", e)
        return {"all": [], "micro": [], "mid": [], "large": []}

    # 流通市值分层（用最新 daily_basic）
    cap_tier = {}
    try:
        db = provider.get_daily_basic(end_date)
        if db is not None and not db.empty:
            for ts, cmv in zip(db["ts_code"], pd.to_numeric(db["circ_mv"], errors="coerce")):
                if pd.isna(cmv):
                    continue
                yi = cmv / 10000
                cap_tier[ts] = "micro" if yi < _CAP_MICRO else ("large" if yi > _CAP_LARGE else "mid")
    except Exception:
        pass

    micro = [c for c, t in cap_tier.items() if t == "micro"]
    mid = [c for c, t in cap_tier.items() if t == "mid"]
    large = [c for c, t in cap_tier.items() if t == "large"]

    dates_idx = list(close_m.index)
    out = {"all": [], "micro": [], "mid": [], "large": []}
    for d in range_dates:
        if d not in dates_idx:
            for k in out:
                out[k].append(None)
            continue
        pos = dates_idx.index(d)
        if pos < 4:
            for k in out:
                out[k].append(None)
            continue
        window = close_m.iloc[pos - 4: pos + 1]
        ma5 = window.mean()
        today_close = close_m.iloc[pos]
        above = today_close > ma5

        def ratio(codes):
            sub = [c for c in codes if c in above.index]
            valid = above[sub].dropna()
            return round(float(valid.mean()) * 100, 1) if len(valid) else None

        valid_all = above.dropna()
        out["all"].append(round(float(valid_all.mean()) * 100, 1) if len(valid_all) else None)
        out["micro"].append(ratio(micro))
        out["mid"].append(ratio(mid))
        out["large"].append(ratio(large))
    return out


def _index_series(provider, end_date: str, range_dates: list[str]) -> dict:
    """各指数在区间内的累计涨跌幅（相对区间首日）。"""
    series = {}
    date_set = set(range_dates)
    for name, code in _INDICES:
        try:
            idf = provider.get_index_daily(code, end_date)
            if idf is None or idf.empty:
                continue
            idf = idf.sort_values("trade_date")
            idf = idf[idf["trade_date"].astype(str).isin(date_set)]
            if idf.empty:
                continue
            closes = pd.to_numeric(idf["close"], errors="coerce").tolist()
            base = closes[0]
            cum = [round((c / base - 1) * 100, 2) if base else None for c in closes]
            series[name] = cum
        except Exception as e:
            logger.debug("[情绪] 指数%s失败: %s", name, e)
    return series


def _build_kpi(per_day, lianban_series, breadth, last, prev) -> dict:
    """最新交易日 KPI。"""
    d = per_day[last]
    amt = d["amount_yi"]
    amt_prev = per_day[prev]["amount_yi"]
    up, down = d["up"], d["down"]
    lu, ld = d["limit_up"], d["limit_down"]
    height = lianban_series["height"][-1] if lianban_series["height"] else 0

    # 情绪温度（0-100）：综合涨停、广度、量能、涨跌比
    b5 = breadth["all"][-1] if breadth["all"] and breadth["all"][-1] is not None else 50
    temp = 0
    temp += min(lu / 200 * 30, 30)               # 涨停占比
    temp += b5 / 100 * 30                          # 广度
    temp += min(amt / 30000 * 20, 20)             # 量能(3万亿满分)
    temp += min((up / max(down, 1)) / 3 * 20, 20)  # 涨跌比
    temp = round(min(temp, 100))

    return {
        "date": f"{last[:4]}-{last[4:6]}-{last[6:]}",
        "temp": temp,
        "amount_wy": round(amt / 10000, 2),               # 万亿
        "amount_chg_yi": round(amt - amt_prev, 0),        # 环比(亿)
        "lianban_height": height,
        "limit_up": lu, "limit_down": ld,
        "b3": lianban_series["b3"][-1] if lianban_series["b3"] else 0,
        "b4": lianban_series["b4"][-1] if lianban_series["b4"] else 0,
        "b5p": lianban_series["b5p"][-1] if lianban_series["b5p"] else 0,
        "ad_ratio": round(up / max(down, 1), 2),
        "up_count": up, "down_count": down,
    }
