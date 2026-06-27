"""全市场盘中异动雷达：用新浪批量报价扫全市场，算热点板块 / 涨跌幅榜 / 涨停 / 涨跌家数。

约束诚实：东财全市场快照对云IP封禁不可用；新浪批量报价可用但有~1.5秒/批限频，
扫全市场(~5500只)约15-27秒。故采用【后台扫描进缓存·页面秒读缓存(serve-stale)】：
请求时若缓存过期则后台起线程刷新、立即返回当前缓存，避免页面干等。

聚合为纯函数 `_aggregate_radar`(零网络可单测)。判定为盘面事实，不预测涨跌。
"""

from __future__ import annotations

import logging
import threading
import time

import pandas as pd

logger = logging.getLogger(__name__)

_BATCH = 500            # 新浪每批代码数(URL不超长)
_CACHE_TTL = 90         # 缓存有效期(秒)·盘中约每90秒后台刷新
_MIN_SECTOR_N = 3       # 板块至少成分数(统计稳)

_RADAR: dict = {"data": None, "ts": 0.0}
_SCANNING = {"on": False}


def _chunked_quotes(provider, codes: list[str], size: int = _BATCH) -> pd.DataFrame:
    """分批拉新浪实时报价并拼接(单批失败跳过)。"""
    frames = []
    for i in range(0, len(codes), size):
        try:
            q = provider.get_realtime_quote(codes[i:i + size])
            if q is not None and not q.empty:
                frames.append(q)
        except Exception:
            continue
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _aggregate_radar(df: pd.DataFrame, industry_map: dict, limit_fn) -> dict:
    """全市场报价 → 热点板块/涨跌幅榜/涨停/涨跌家数(纯函数·可单测)。

    Args:
        df: 含 ts_code/name/price/pct_chg 的全市场实时报价。
        industry_map: {ts_code: 申万二级行业}。
        limit_fn: (ts_code, name) -> 涨停幅%（板块感知）。
    """
    if df is None or df.empty:
        return {"hot_sectors": [], "gainers": [], "losers": [], "limit_ups": [], "breadth": {}}
    d = df.copy()
    d["pct"] = pd.to_numeric(d["pct_chg"], errors="coerce")
    d = d.dropna(subset=["pct"])
    d["ind"] = d["ts_code"].map(industry_map).fillna("")
    d["is_st"] = d["name"].astype(str).str.upper().str.contains("ST")
    d["lim"] = [limit_fn(c, n) for c, n in zip(d["ts_code"], d["name"].astype(str))]
    d["is_limit_up"] = d["pct"] >= (d["lim"] - 0.3)
    d["is_limit_down"] = d["pct"] <= -(d["lim"] - 0.3)

    def _row(r):
        return {"name": str(r["name"]), "code": str(r["ts_code"]), "pct": round(float(r["pct"]), 2),
                "price": (round(float(r["price"]), 2) if pd.notna(r.get("price")) else None),
                "industry": r["ind"], "is_st": bool(r["is_st"])}

    # 热点板块：行业均涨幅(成分≥N)·领涨龙头=板块内涨幅最高
    hot = []
    for ind, g in d[d["ind"] != ""].groupby("ind"):
        if len(g) < _MIN_SECTOR_N:
            continue
        lead = g.nlargest(1, "pct").iloc[0]
        hot.append({"industry": ind, "n": int(len(g)), "avg_pct": round(float(g["pct"].mean()), 2),
                    "up": int((g["pct"] > 0).sum()), "limit_up": int(g["is_limit_up"].sum()),
                    "leader": str(lead["name"]), "leader_code": str(lead["ts_code"]),
                    "leader_pct": round(float(lead["pct"]), 2)})
    hot.sort(key=lambda x: -x["avg_pct"])

    return {
        "hot_sectors": hot[:14],
        "weak_sectors": sorted(hot, key=lambda x: x["avg_pct"])[:6],
        "gainers": [_row(r) for _, r in d.nlargest(18, "pct").iterrows()],
        "losers": [_row(r) for _, r in d.nsmallest(12, "pct").iterrows()],
        "limit_ups": [_row(r) for _, r in d[d["is_limit_up"]].nlargest(40, "pct").iterrows()],
        "breadth": {"total": int(len(d)), "up": int((d["pct"] > 0).sum()),
                    "down": int((d["pct"] < 0).sum()), "flat": int((d["pct"] == 0).sum()),
                    "limit_up": int(d["is_limit_up"].sum()), "limit_down": int(d["is_limit_down"].sum())},
    }


def _active_universe(provider, n: int) -> list[str]:
    """取最新因子表中成交额前 n 只（活跃股池）；无则回退全市场。

    板块由其流动性龙头驱动，扫活跃股池基本不漏热点，且只扫 ~n 只 → 快很多、可高频。
    """
    try:
        import glob

        from app.config import get_settings
        from app.strategy.screener import _FACTOR_TABLE_VERSION
        files = sorted(glob.glob(str(get_settings().cache_dir / "factor_table"
                                     / f"*_{_FACTOR_TABLE_VERSION}.parquet")))
        if files:
            df = pd.read_parquet(files[-1], columns=["ts_code", "amount_100m"])
            top = df.nlargest(n, "amount_100m")["ts_code"].tolist()
            if top:
                return top
    except Exception as e:
        logger.debug("[雷达] 活跃股池获取失败，回退全市场: %s", e)
    return provider.get_stock_basic()["ts_code"].tolist()


def build_market_radar(provider=None, top_active: int | None = None) -> dict:
    """扫市场(分批新浪)→ 聚合异动雷达。

    top_active=None 扫全市场(~5500只·~20秒·页面用)；传 N 则只扫成交额前 N 的活跃股池
    (~5秒·可高频·盯盘推送用)。
    """
    from app.data.composite_provider import CompositeProvider
    from app.nodes.quick_report import _board_limit_pct
    provider = provider or CompositeProvider()
    sb = provider.get_stock_basic()
    industry_map = dict(zip(sb["ts_code"], sb["industry"].fillna("")))
    codes = _active_universe(provider, top_active) if top_active else sb["ts_code"].tolist()
    df = _chunked_quotes(provider, codes)
    return _aggregate_radar(df, industry_map, _board_limit_pct)


def _scan_into_cache(provider=None) -> None:
    try:
        data = build_market_radar(provider)
        _RADAR["data"], _RADAR["ts"] = data, time.time()
    except Exception as e:
        logger.exception("市场雷达扫描失败: %s", e)
    finally:
        _SCANNING["on"] = False


def get_market_radar(provider=None) -> dict:
    """秒读缓存：缓存过期则后台起线程刷新、立即返回当前缓存(首次返回 scanning)。"""
    now = time.time()
    fresh = _RADAR["data"] is not None and (now - _RADAR["ts"] < _CACHE_TTL)
    if not fresh and not _SCANNING["on"]:
        _SCANNING["on"] = True
        threading.Thread(target=_scan_into_cache, args=(provider,), daemon=True).start()
    data = _RADAR["data"]
    return {"ok": True, "scanning": data is None and _SCANNING["on"],
            "as_of": (time.strftime("%H:%M:%S", time.localtime(_RADAR["ts"])) if _RADAR["ts"] else ""),
            **(data or {"hot_sectors": [], "gainers": [], "losers": [], "limit_ups": [], "breadth": {}})}
