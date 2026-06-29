"""大盘体检 · 多维同轴数据层。

把已有的大盘情绪序列(build_dashboard) + 板块轮动 + 信号事件研究拼成"一张大局图"
所需的结构化数据。设计目标：**让规律在同一根时间轴上自己浮现**，而不是再堆一页数字。

三个视角各回答一个交易决策：
- 多维同轴(该不该重仓)：指数/成交额/市场广度/净涨停/连板高度 对齐 + 每日市场状态带 + 地量+广度冰点信号。
- 板块轮动(买哪条线)：申万二级行业 × 时间 的日涨幅矩阵，主线迁移一眼可见。
- 信号复盘(规律靠不靠谱)：把每次"地量+广度冰点"对齐到第0天，看其后大盘走势 + 胜率/样本量。

诚实铁律：
- 复用 build_dashboard 的真实序列(Tushare)，不臆造数字。
- 市场状态/信号阈值是**经验派生**(非保证)，样本量(n)如实标注，小样本不吹胜率。
- 不预测涨跌、不构成买卖建议。
"""

from __future__ import annotations

import datetime
import logging

from app.data.composite_provider import CompositeProvider
from app.strategy.market_sentiment import build_dashboard

logger = logging.getLogger(__name__)

# —— 市场状态阈值（派生自节点A口径，用 build_dashboard 现成字段：涨停/跌停家数 + 5日线占比广度）——
_STRONG_LU, _STRONG_BREADTH, _STRONG_LD_MAX = 60, 55.0, 15   # 强：涨停多+广度高+几乎无跌停
_WEAK_LD, _WEAK_BREADTH = 30, 35.0                            # 弱：跌停多 或 广度冰点

# —— 地量+广度冰点 信号阈值（经验·可调）——
_DRYUP_AMT_PCT = 0.25       # 成交额处于窗口最低 25% 分位 = 地量
_ICE_BREADTH = 40.0         # 5日线占比 ≤40% = 广度偏冰点


def net_limit_series(limit_up: list, limit_down: list) -> list[int]:
    """净涨停 = 涨停家数 − 跌停家数（情绪强弱的最直观一条线）。"""
    return [int((u or 0) - (d or 0)) for u, d in zip(limit_up, limit_down)]


def regime_series(limit_up: list, limit_down: list, breadth_all: list) -> list[dict]:
    """逐日市场状态：强 / 震荡 / 弱（经验派生·非保证）。返回每日 {label, color}。"""
    out: list[dict] = []
    for u, d, b in zip(limit_up, limit_down, breadth_all):
        u, d = int(u or 0), int(d or 0)
        bb = float(b) if b is not None else 50.0
        if u >= _STRONG_LU and bb >= _STRONG_BREADTH and d < _STRONG_LD_MAX:
            out.append({"label": "强", "color": "strong"})
        elif d >= _WEAK_LD or bb <= _WEAK_BREADTH:
            out.append({"label": "弱", "color": "weak"})
        else:
            out.append({"label": "震荡", "color": "mid"})
    return out


def detect_dryup_signals(amount: list, breadth: list,
                         *, amt_pct: float = _DRYUP_AMT_PCT,
                         breadth_th: float = _ICE_BREADTH) -> list[int]:
    """地量 + 广度冰点 信号日（成交额窗口低分位 且 广度≤冰点 且 为局部成交地量）。返回索引列表。"""
    vals = [a for a in amount if a is not None]
    if len(vals) < 10:
        return []
    amt_thresh = sorted(vals)[max(0, int(len(vals) * amt_pct) - 1)]
    raw: list[int] = []
    for i in range(2, len(amount) - 1):
        a, b = amount[i], breadth[i]
        if a is None or b is None:
            continue
        prev_a, next_a = amount[i - 1], amount[i + 1]
        is_local_low = a <= (prev_a if prev_a is not None else a) and a <= (next_a if next_a is not None else a)
        if a <= amt_thresh and b <= breadth_th and is_local_low:
            raw.append(i)
    collapsed: list[int] = []
    for s in raw:                                    # 合并相邻信号（5日内只留一个）
        if not collapsed or s - collapsed[-1] > 5:
            collapsed.append(s)
    return collapsed


def _median(xs: list) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2, 2)


def event_study(index_cum: list, sig_idx: list[int],
                *, pre: int = 3, horizon: int = 10) -> dict:
    """把每个信号对齐到第0天，取 [-pre, +horizon] 的指数相对涨幅路径 + 均值 + T+5 胜率/中位。

    诚实：index_cum 是相对首日累计%，路径再减信号日的值=以信号日为0基准。小样本如实返回 n。
    """
    rel = list(range(-pre, horizon + 1))
    paths: list[list[float]] = []
    for s in sig_idx:
        if s - pre < 0 or s + horizon >= len(index_cum):
            continue
        base = index_cum[s]
        if base is None:
            continue
        row, ok = [], True
        for k in rel:
            v = index_cum[s + k]
            if v is None:
                ok = False
                break
            row.append(round(v - base, 2))
        if ok:
            paths.append(row)
    n = len(paths)
    mean = [round(sum(p[j] for p in paths) / n, 2) for j in range(len(rel))] if n else []
    t5 = rel.index(5) if 5 in rel else None
    wins = sum(1 for p in paths if t5 is not None and p[t5] > 0) if n else 0
    return {"rel_days": rel, "paths": paths, "mean": mean, "n": n,
            "winrate_t5": round(wins / n * 100) if n else None,
            "median_t5": _median([p[t5] for p in paths]) if (n and t5 is not None) else None}


def _sector_matrix(provider: CompositeProvider, dates: list[str], *, top: int = 14) -> dict:
    """申万二级行业 × 时间 的日涨幅矩阵（取窗口内成交额最大的 top 个活跃行业·主线轮动用）。"""
    try:
        basic = provider.get_stock_basic()
        ind = dict(zip(basic["ts_code"], basic["industry"]))
    except Exception as e:
        logger.warning("[体检] 行业映射失败: %s", e)
        return {"names": [], "dates": dates, "matrix": []}

    day_ret: dict[str, dict] = {}
    amt_sum: dict[str, float] = {}
    for d in dates:
        dd = provider.get_daily(d)                   # 已按日缓存·复用 build_dashboard 拉过的数据
        if dd is None or dd.empty:
            continue
        df = dd[["ts_code", "pct_chg", "amount"]].copy()
        df["ind"] = df["ts_code"].map(ind)
        df = df.dropna(subset=["ind"])
        grp = df.groupby("ind")
        day_ret[d] = grp["pct_chg"].mean().round(2).to_dict()
        for k, v in grp["amount"].sum().items():
            amt_sum[k] = amt_sum.get(k, 0.0) + float(v)

    names = [k for k, _ in sorted(amt_sum.items(), key=lambda x: -x[1])[:top]]
    matrix = [[day_ret.get(d, {}).get(nm) for d in dates] for nm in names]
    return {"names": names, "dates": dates, "matrix": matrix}


def build_overview(end_date: str = "", panel_days: int = 60,
                   sector_days: int = 24, force: bool = False) -> dict:
    """编排：复用大盘情绪序列 + 板块轮动矩阵 + 信号事件研究 → 一张大局图所需的全部数据。

    用 build_dashboard 的 start_date 区间路径取窗口（避开 _recent_trade_dates 的 25 自然日硬上限，
    那条共享函数被 20+ 处依赖·不擅动）；start_date 按 panel_days 反推自然日，足够覆盖 N 个交易日。
    """
    provider = CompositeProvider()
    end_date = end_date or datetime.date.today().strftime("%Y%m%d")
    lookback_cal = int(panel_days * 1.55) + 12          # 交易日≈日历日×5/7·留节假日缓冲
    start_date = (datetime.datetime.strptime(end_date, "%Y%m%d")
                  - datetime.timedelta(days=lookback_cal)).strftime("%Y%m%d")
    dash = build_dashboard(end_date, start_date=start_date, force=force)

    dates = dash.get("dates", [])
    amount = dash.get("amount", [])
    breadth = (dash.get("breadth") or {}).get("all", [])
    index_cum = (dash.get("indices") or {}).get("上证", [])
    lu, ld = dash.get("limit_up", []), dash.get("limit_down", [])
    height = (dash.get("lianban") or {}).get("height", [])

    sigs = detect_dryup_signals(amount, breadth)
    sector = _sector_matrix(provider, dates[-sector_days:]) if dates else {"names": [], "dates": [], "matrix": []}

    return {
        "end_date": dates[-1] if dates else end_date,
        "dates": dates,
        "index_cum": index_cum,
        "amount": amount,
        "breadth": breadth,
        "net_limit": net_limit_series(lu, ld),
        "lianban_height": height,
        "regime": regime_series(lu, ld, breadth),
        "regime_now": dash.get("regime", {}),
        "kpi": dash.get("kpi", {}),
        "signals": [{"i": i, "date": dates[i]} for i in sigs],
        "event": event_study(index_cum, sigs),
        "sectors": sector,
        "hot_themes": dash.get("hot_themes", []),
    }
