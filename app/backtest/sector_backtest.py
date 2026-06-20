"""
同类/板块回测（阶段二）：把个股回测放到"它的同类一篮子票"里看，回答两个实战问题。

③ 同类对比：同一信号在同行业一篮子票上的**汇总胜率**（大样本基准）→ 本股 edge 是
   个股独有 alpha，还是整个板块共性？
④ 板块广度：板块内"% 站上 MA5 / MA20"随时间的曲线（内部健康度，戳穿"指数被权重股
   顶住的虚强"）+ 把汇总信号按"触发时板块广度"分桶（大样本，统计有效）。

设计：
- 同类 = 当前同 `stock_basic.industry`、非 ST、按 `daily_basic.circ_mv` 取市值前 N（代表性）。
  ⚠️ 用当前成分/市值定义历史样本，含一定幸存者偏差，作参考。
- ③④共用一套"板块成分价格序列"：拉一次即同时算汇总胜率与广度（④是③的免费副产品）。
- 纯统计聚合（_agg），不输出"胜率排序选股"，符合项目禁止项。
"""

from __future__ import annotations

import datetime
import logging

import pandas as pd

from app.backtest.signal_backtest import (
    HORIZONS, _agg, _custom_signal_def, _signal_defs,
)
from app.data.composite_provider import CompositeProvider
from app.data.kline_loader import load_kline

logger = logging.getLogger(__name__)

DEFAULT_MAX_PEERS = 30

# 板块广度（% 站上 MA20）分档：判定信号触发时板块内部强弱
_BREADTH_BANDS = [(60, 101, "板块强(≥60%)"), (40, 60, "板块中性(40-60%)"), (0, 40, "板块弱(<40%)")]


# ──────────────────────────────────────────────
# 同类成分解析
# ──────────────────────────────────────────────

def _ref_trade_date(provider, end: str) -> str:
    """end 当日或之前最近的交易日（用于取市值快照）。"""
    start = (datetime.datetime.strptime(end, "%Y%m%d") - datetime.timedelta(days=20)).strftime("%Y%m%d")
    try:
        cal = provider.get_trade_cal(start, end)
        opens = sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist())
        return opens[-1] if opens else end
    except Exception:
        return end


def _market_cap_map(provider, end: str) -> dict[str, float]:
    """{ts_code: 流通市值}（取最近交易日 daily_basic.circ_mv）；失败返回空。"""
    try:
        db = provider.get_daily_basic(_ref_trade_date(provider, end))
        if db is None or db.empty or "circ_mv" not in db.columns:
            return {}
        return dict(zip(db["ts_code"].astype(str), pd.to_numeric(db["circ_mv"], errors="coerce")))
    except Exception:
        return {}


def _resolve_peers(provider, ts_code: str, end: str, max_peers: int) -> tuple[str, list[tuple[str, str]]]:
    """返回 (行业名, [(peer_code, peer_name)...])：同行业、非 ST、按市值取前 N、剔除自身。"""
    sb = provider.get_stock_basic()
    if sb is None or "industry" not in sb.columns:
        return "", []
    row = sb[sb["ts_code"].astype(str) == ts_code]
    if row.empty:
        return "", []
    ind = str(row.iloc[0]["industry"])
    if not ind or ind == "nan":
        return "", []
    peers = sb[(sb["industry"].astype(str) == ind) & (sb["ts_code"].astype(str) != ts_code)].copy()
    peers = peers[~peers["name"].astype(str).str.contains("ST", na=False)]
    if peers.empty:
        return ind, []
    mv = _market_cap_map(provider, end)
    peers["_mv"] = peers["ts_code"].astype(str).map(mv).fillna(0.0)
    peers = peers.sort_values("_mv", ascending=False).head(max_peers)
    return ind, list(zip(peers["ts_code"].astype(str), peers["name"].astype(str)))


# ──────────────────────────────────────────────
# 板块广度（纯函数，可单测）
# ──────────────────────────────────────────────

def sector_breadth(series_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    成分日线 {code: kline(含trade_date,close)} → DataFrame[index=date, pct_ma20, pct_ma5]。
    某日广度 = 成分中"收盘 > MAx"的占比（仅计该日 MAx 有效的成分）。
    """
    closes = {c: pd.to_numeric(k.set_index("trade_date")["close"], errors="coerce")
              for c, k in series_map.items() if k is not None and not k.empty}
    if not closes:
        return pd.DataFrame(columns=["pct_ma20", "pct_ma5"])
    mat = pd.DataFrame(closes).sort_index()
    ma20, ma5 = mat.rolling(20).mean(), mat.rolling(5).mean()
    b20 = (mat > ma20).where(ma20.notna()).mean(axis=1) * 100
    b5 = (mat > ma5).where(ma5.notna()).mean(axis=1) * 100
    return pd.DataFrame({"pct_ma20": b20.round(1), "pct_ma5": b5.round(1)})


def _breadth_band(pct: float) -> str:
    for lo, hi, label in _BREADTH_BANDS:
        if lo <= pct < hi:
            return label
    return _BREADTH_BANDS[0][2] if pct >= 60 else _BREADTH_BANDS[-1][2]


# ──────────────────────────────────────────────
# 单股信号出现点（防未来函数，复用主引擎口径）
# ──────────────────────────────────────────────

def _occurrences(k: pd.DataFrame, sd: dict, start: str) -> list[dict]:
    """单股信号出现点：[{date, rets:{h:%}}]，买入=次日开盘、卖出=T+N收盘。"""
    if k is None or k.empty or len(k) < sd["min_bars"] + 2:
        return []
    opens = k["open"].astype(float).tolist()
    closes = k["close"].astype(float).tolist()
    dates = k["trade_date"].astype(str).tolist()
    n, out = len(k), []
    for i in range(sd["min_bars"] - 1, n - 1):
        if dates[i] < start:
            continue
        try:
            if not sd["detect"](k.iloc[: i + 1]):
                continue
        except Exception:
            continue
        entry = opens[i + 1]
        if entry <= 0:
            continue
        rets = {h: round((closes[i + h] - entry) / entry * 100, 2)
                for h in HORIZONS if i + h < n and closes[i + h] > 0}
        if rets:
            out.append({"date": dates[i], "rets": rets})
    return out


# ──────────────────────────────────────────────
# 汇总：同类基准 + 广度分桶
# ──────────────────────────────────────────────

def _pool_stats(series_map: dict[str, pd.DataFrame], sd: dict, start: str,
                breadth_map: dict[str, float]) -> dict:
    """池化所有同类票的信号出现点：按持有期汇总 + 按触发时板块广度分桶。"""
    pooled = {h: [] for h in HORIZONS}
    by_band = {lbl: {h: [] for h in HORIZONS} for *_, lbl in _BREADTH_BANDS}
    band_n = {lbl: 0 for *_, lbl in _BREADTH_BANDS}
    total = 0
    for k in series_map.values():
        for o in _occurrences(k, sd, start):
            total += 1
            for h, r in o["rets"].items():
                pooled[h].append(r)
            b = breadth_map.get(o["date"])
            if b is not None and not pd.isna(b):
                lbl = _breadth_band(float(b))
                band_n[lbl] += 1
                for h, r in o["rets"].items():
                    by_band[lbl][h].append(r)
    return {
        "n_occ": total,
        "pooled": {h: _agg(h, pooled[h]).__dict__ for h in HORIZONS},
        "by_breadth": {lbl: {"n": band_n[lbl],
                             "horizons": {h: _agg(h, by_band[lbl][h]).__dict__ for h in HORIZONS}}
                       for *_, lbl in _BREADTH_BANDS if band_n[lbl] > 0},
    }


def _breadth_curve(breadth: pd.DataFrame, start: str, max_points: int = 240) -> list[dict]:
    """广度曲线（裁到 [start, ∞)、降采样到 ~max_points 点）。"""
    df = breadth[breadth.index.astype(str) >= start].dropna(how="all")
    if df.empty:
        return []
    step = max(1, len(df) // max_points)
    df = df.iloc[::step]
    return [{"date": str(idx),
             "ma20": None if pd.isna(r["pct_ma20"]) else r["pct_ma20"],
             "ma5": None if pd.isna(r["pct_ma5"]) else r["pct_ma5"]}
            for idx, r in df.iterrows()]


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

def analyze_sector(ts_code: str, signal_key: str, start: str, end: str,
                   provider: CompositeProvider | None = None, custom: dict | None = None,
                   max_peers: int = DEFAULT_MAX_PEERS) -> dict:
    """
    同类/板块分析。返回同类基准(③) + 板块广度曲线/当前广度 + 信号×广度分桶(④)。

    首次拉取同类成分序列较慢（~1分钟级），之后命中缓存秒开。
    """
    sd = _custom_signal_def(custom) if custom else _signal_defs().get(signal_key)
    if not sd:
        return {"ok": False, "msg": "未知信号"}
    provider = provider or CompositeProvider()

    ind, peers = _resolve_peers(provider, ts_code, end, max_peers)
    if not peers:
        return {"ok": False, "msg": f"未找到同类票（行业：{ind or '未知'}）"}

    buf_start = (datetime.datetime.strptime(start, "%Y%m%d")
                 - datetime.timedelta(days=200)).strftime("%Y%m%d")
    series_map: dict[str, pd.DataFrame] = {}
    for code, _name in peers:
        try:
            k = load_kline(code, buf_start, end, provider, adj="qfq")
            if k is not None and not k.empty:
                series_map[code] = k
        except Exception:
            logger.warning("同类票 %s 加载失败，跳过", code)
    if not series_map:
        return {"ok": False, "msg": "同类票历史数据加载失败"}

    breadth = sector_breadth(series_map)
    breadth_map = breadth["pct_ma20"].to_dict() if not breadth.empty else {}
    stats = _pool_stats(series_map, sd, start, breadth_map)
    cur = breadth.dropna(how="all")
    current = {"pct_ma20": None, "pct_ma5": None}
    if not cur.empty:
        last = cur.iloc[-1]
        current = {"pct_ma20": None if pd.isna(last["pct_ma20"]) else float(last["pct_ma20"]),
                   "pct_ma5": None if pd.isna(last["pct_ma5"]) else float(last["pct_ma5"])}

    return {
        "ok": True,
        "industry": ind,
        "signal_label": sd["label"],
        "n_peers": len(series_map),
        "peers_sample": [n for _c, n in peers][:12],
        "n_occ": stats["n_occ"],
        "pooled": stats["pooled"],          # ③ 同类基准
        "by_breadth": stats["by_breadth"],  # ④ 信号×广度分桶
        "breadth_curve": _breadth_curve(breadth, start),
        "current_breadth": current,
    }
