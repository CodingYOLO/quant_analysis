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

# 展示用指数（吴川分类口径；均经 Tushare index_daily 实测可取真实数据）
#   全A=中证全指 / 上证=上证指数 / 红利=中证红利 / 小盘=国证2000 /
#   中小盘=中证500 / 深成=深证成指 / 创业板=创业板指
_INDICES = [
    ("全A", "000985.CSI"),     # 中证全指（全A 代理，万得全A Tushare 无）
    ("上证", "000001.SH"),     # 上证指数
    ("红利", "000922.CSI"),    # 中证红利
    ("小盘", "399303.SZ"),     # 国证2000
    ("中小盘", "000905.SH"),   # 中证500
    ("深成", "399001.SZ"),     # 深证成指
    ("创业板", "399006.SZ"),   # 创业板指
]

# 流通市值分层（亿元）
_CAP_MICRO = 50      # < 50亿 微盘
_CAP_LARGE = 300     # > 300亿 大盘；中间为中盘


def _cache_path(end_date: str, days: int, start_date: str = "") -> Path:
    settings = get_settings()
    p = settings.cache_dir / "sentiment"
    p.mkdir(parents=True, exist_ok=True)
    tag = f"{start_date}_{end_date}" if start_date else f"{end_date}_{days}"
    return p / f"{tag}.json"


def _trade_dates_between(provider, start_date: str, end_date: str) -> list[str]:
    """区间内所有交易日（升序），并向前多取 ~15 个交易日用于连板回溯（避免高连板被低估）。"""
    import datetime
    # 向前扩 ~15 个交易日 ≈ 30 自然日（覆盖 7 板+ 的回溯深度）
    pad_start = (datetime.datetime.strptime(start_date, "%Y%m%d") - datetime.timedelta(days=30)).strftime("%Y%m%d")
    cal = provider.get_trade_cal(pad_start, end_date)
    return sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist())


def _latest_data_date(provider, end_date: str) -> str:
    """返回 ≤ end_date 且已有日线数据的最近交易日（用于判断缓存是否过期）。"""
    try:
        dates = _recent_trade_dates(provider, end_date, n=4)
    except Exception:
        return end_date
    for d in reversed(dates):   # 由新到旧，取第一个有数据的
        try:
            dd = provider.get_daily(d)
            if dd is not None and not dd.empty:
                return d
        except Exception:
            continue
    return end_date


def build_dashboard(end_date: str, days: int = 22, start_date: str = "", force: bool = False) -> dict:
    """
    构建大盘情绪仪表盘数据。
    指定 start_date 则按区间 [start_date, end_date]；否则取 end_date 往前 days 个交易日。
    结果按缓存键缓存。
    """
    provider = CompositeProvider()
    # 最新「有数据」的交易日：缓存必须覆盖到它，否则视为过期重建
    # （修复：缓存在当日数据未入库时生成会冻结在昨日，且大中小盘广度因 daily_basic 缺失全空）
    latest = _latest_data_date(provider, end_date)
    path = _cache_path(end_date, days, start_date)
    if path.exists() and not force:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("dates") and cached["dates"][-1] == latest:
                return cached
            logger.info("[情绪] 缓存过期：缓存末日 %s ≠ 最新数据日 %s，重建",
                        (cached.get("dates") or ["?"])[-1], latest)
        except Exception:
            pass
    if start_date:
        all_dates = _trade_dates_between(provider, start_date, end_date)
    else:
        # 取 days + 15 个交易日（多15天用于连板回溯，避免高连板被低估），升序
        all_dates = _recent_trade_dates(provider, end_date, n=days + 15)
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

    # 指定区间：取 [start_date, end_date] 全部交易日（不截断）；否则取最近 days 天
    if start_date:
        range_dates = [d for d in all_dates if d in per_day and d >= start_date]
    else:
        range_dates = [d for d in all_dates if d in per_day][-days:]

    # —— 连板梯队 + 涨跌停：优先官方 limit_list_d（准确·含炸板），失败回退日线推断 ——
    lianban_series = _lianban_series(all_dates, limit_sets, range_dates)
    official = _official_limit_series(provider, range_dates)
    if official:
        for i, d in enumerate(range_dates):
            if d in per_day:
                per_day[d]["limit_up"] = official["limit_up"][i]
                per_day[d]["limit_down"] = official["limit_down"][i]
        lianban_series = official["lianban"]
    limit_official = (official or {}).get("latest", {})

    # —— 市场广度（5日线占比，全市场+大中小盘）——
    breadth = _breadth_series(provider, end_date, range_dates)

    # —— 指数涨跌幅 ——
    indices = _index_series(provider, end_date, range_dates)

    # —— KPI（最新交易日，连板/涨跌停已用官方）——
    last = range_dates[-1]
    prev = range_dates[-2] if len(range_dates) >= 2 else last
    kpi = _build_kpi(per_day, lianban_series, breadth, last, prev)

    # —— 区间行情类型判断（震荡/牛市/熊市）——
    regime = _classify_market_regime(indices, breadth, per_day, range_dates)

    # —— 龙虎榜 / 游资席位（官方 top_inst）——
    try:
        from app.strategy.market_extras import get_dragon_tiger
        lhb = _lhb_summary(get_dragon_tiger(range_dates[-1], provider), code2name)
    except Exception:
        logger.warning("[sentiment] 龙虎榜数据失败", exc_info=True)
        lhb = {}

    # —— 今日热点题材（开盘啦打板榜单 kpl_list）——
    try:
        hot_themes = _hot_themes(provider.get_kpl_list(range_dates[-1]))
    except Exception:
        logger.warning("[sentiment] 热点题材数据失败", exc_info=True)
        hot_themes = []

    result = {
        "end_date": end_date,
        "regime": regime,
        "kpi": kpi,
        "dates": range_dates,
        "amount": [round(per_day[d]["amount_yi"] / 10000, 3) for d in range_dates],  # 万亿
        "limit_up": [per_day[d]["limit_up"] for d in range_dates],
        "limit_down": [per_day[d]["limit_down"] for d in range_dates],
        "lianban": lianban_series,
        "breadth": breadth,
        "indices": indices,
        "limit_official": limit_official,
        "lhb": lhb,
        "hot_themes": hot_themes,
    }
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def _classify_market_regime(indices: dict, breadth: dict, per_day: dict, range_dates: list[str]) -> dict:
    """
    判断区间整体行情类型：牛市行情 / 熊市行情 / 震荡行情。
    依据：上证区间累计涨幅 + 平均市场广度 + 涨跌停净值趋势。
    """
    # 上证区间累计涨幅（_index_series 已是相对首日的累计%）
    sh = indices.get("上证") or []
    sh_cum = sh[-1] if sh and sh[-1] is not None else 0.0
    # 平均广度
    bvals = [b for b in breadth.get("all", []) if b is not None]
    avg_breadth = sum(bvals) / len(bvals) if bvals else 50.0
    # 区间涨停-跌停净值均值（情绪强弱）
    net_limit = [per_day[d]["limit_up"] - per_day[d]["limit_down"] for d in range_dates if d in per_day]
    avg_net = sum(net_limit) / len(net_limit) if net_limit else 0

    if sh_cum >= 5 and avg_breadth >= 50:
        label, color = "牛市行情", "up"
        reason = f"上证区间涨{sh_cum:+.1f}%，平均广度{avg_breadth:.0f}%偏高，赚钱效应强"
    elif sh_cum <= -8 or avg_breadth < 25:
        label, color = "熊市行情", "down"
        reason = f"上证区间{sh_cum:+.1f}%，平均广度仅{avg_breadth:.0f}%，赚钱效应弱"
    else:
        label, color = "震荡行情", "amber"
        reason = f"上证区间{sh_cum:+.1f}%、平均广度{avg_breadth:.0f}%，多空胶着无明确趋势"
    return {"label": label, "color": color, "reason": reason,
            "sh_cum": round(sh_cum, 1), "avg_breadth": round(avg_breadth, 1), "avg_net_limit": round(avg_net, 0)}


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


def _lianban_dist(up_df) -> tuple[dict, int]:
    """官方涨停榜 DataFrame → ({连板数: 家数}, 最高连板)。limit_times 即连板数。"""
    if up_df is None or up_df.empty or "limit_times" not in up_df.columns:
        return {}, 0
    lt = pd.to_numeric(up_df["limit_times"], errors="coerce").fillna(1).astype(int)
    dist: dict[int, int] = {}
    for v in lt:
        if v >= 2:
            dist[int(v)] = dist.get(int(v), 0) + 1
    return dist, int(lt.max()) if len(lt) else 0


def _official_limit_series(provider, range_dates: list[str]) -> dict | None:
    """
    官方 limit_list_d 逐日序列：涨停/跌停家数 + 连板梯队(b3/b4/b5p/height) + 最新日完整(含炸板)。
    比日线"±涨幅推断"准（能分清涨停/炸板、连板晋级）。任一日失败即回退日线（返回 None）。
    """
    if not range_dates:
        return None
    lu, ld, b3, b4, b5p, height = ([] for _ in range(6))
    try:
        for d in range_dates:
            up = provider.get_limit_list(d, "U")
            down = provider.get_limit_list(d, "D")
            lu.append(int(len(up)) if up is not None else 0)
            ld.append(int(len(down)) if down is not None else 0)
            dist, mx = _lianban_dist(up)
            b3.append(dist.get(3, 0))
            b4.append(dist.get(4, 0))
            b5p.append(sum(c for h, c in dist.items() if h >= 5))
            height.append(mx)
    except Exception:
        logger.warning("[sentiment] 官方连板序列失败，回退日线推断", exc_info=True)
        return None
    from app.strategy.market_extras import get_limit_analysis
    latest = get_limit_analysis(range_dates[-1], provider) or {}
    return {"limit_up": lu, "limit_down": ld,
            "lianban": {"b3": b3, "b4": b4, "b5p": b5p, "height": height}, "latest": latest}


def _lhb_summary(dt: dict, code2name: dict) -> dict:
    """龙虎榜明细 → 上榜家数 + 主导力量分布 + 知名游资动向 + 净买额榜(前8)。"""
    if not dt:
        return {}
    dominant_dist: dict[str, int] = {}
    famous: dict[str, list] = {}
    rows = []
    for code, info in dt.items():
        dom = info.get("dominant", "营业部")
        dominant_dist[dom] = dominant_dist.get(dom, 0) + 1
        name = code2name.get(code, "") or code.split(".")[0]
        rows.append({"code": code.split(".")[0], "name": name,
                     "net_yi": info.get("net_buy_yi", 0), "dominant": dom})
        for s in info.get("seats", []):
            if "游资·" in s.get("tag", ""):
                famous.setdefault(s["tag"].replace("🔥游资·", ""), []).append(name)
    rows.sort(key=lambda x: x["net_yi"], reverse=True)
    return {
        "n": len(dt),
        "dominant_dist": dict(sorted(dominant_dist.items(), key=lambda kv: -kv[1])),
        "famous": {k: list(dict.fromkeys(v))[:3] for k, v in list(famous.items())[:6]},  # 去重股名
        "top_net": rows[:8],
    }


def _hot_themes(df, top_n: int = 6) -> list[dict]:
    """开盘啦打板榜单 → 今日热点题材榜：按涨停个股的题材出现次数排名 + 代表股。"""
    if df is None or df.empty or "theme" not in df.columns:
        return []
    up = df[df["tag"] == "涨停"] if "tag" in df.columns else df
    cnt: dict[str, int] = {}
    stocks: dict[str, list] = {}
    for _, r in up.iterrows():
        name = str(r.get("name") or "")
        themes = str(r.get("theme") or "").replace("，", "、").replace(",", "、")
        for t in themes.split("、"):
            t = t.strip()
            if not t or t == "nan":
                continue
            cnt[t] = cnt.get(t, 0) + 1
            stocks.setdefault(t, []).append(name)
    ranked = sorted(cnt.items(), key=lambda kv: -kv[1])[:top_n]
    return [{"theme": t, "count": c, "stocks": list(dict.fromkeys(stocks[t]))[:4]} for t, c in ranked]


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

    # 情绪温度（0-100）：5 维度加权，核心加入"连板高度=持续性"，避免单日涨停多即过热
    #   涨停强度 25 + 市场广度 25 + 连板高度(持续性) 20 + 量能 15 + 上涨家数占比 15
    b5 = breadth["all"][-1] if breadth["all"] and breadth["all"][-1] is not None else 50
    up_ratio = up / max(up + down, 1)            # 上涨家数占比
    temp = 0
    temp += min(lu / 250, 1) * 25                 # 涨停强度（250家封顶）
    temp += b5 / 100 * 25                          # 市场广度（站上5日线占比）
    temp += min(height / 6, 1) * 20               # 连板高度/持续性（6板封顶）
    temp += min(amt / 30000, 1) * 15              # 量能（3万亿封顶）
    temp += max(min((up_ratio - 0.5) / 0.3, 1), 0) * 15  # 上涨占比 0.5~0.8 → 0~15
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
