"""盘感训练（盲测复盘）：抽历史某只票某决策日 T0，展示截至 T0 的日线 + 大盘 + 位置，
让用户判断「买入持有 N 日盈亏」，再揭晓真实后续并评分。

铁律（决定它真不真）：
- **零未来泄漏**：题目里所有数据/指标只用 ≤T0 的 bar 算；T+1..T+N 仅作答案，绝不进题面。
- **蒙眼**：题面不含股票名/日期（防"我记得这票后来涨了"作弊），揭晓时才给。
- **诚实记分**：评分对比随机基准 base rate，样本量 n 如实标。

设计为纯函数 + 注入 provider，核心 `split_at` / `bucket_of` / `score` / `classify_setup`
不连网可单测；`build_quiz` 负责抽样取数编排。
"""

from __future__ import annotations

import datetime
import logging
import random

import pandas as pd

from app.data.kline_loader import load_kline

logger = logging.getLogger(__name__)

# 主预测周期（交易日）。买入持有 N 日，看赚亏 + 幅度档
DEFAULT_FWD = 5
DEFAULT_HIST = 120                       # 题面展示的历史 bar 数

# 持有 N 日收益分档（%）。顺序=从涨到跌，相邻档算"半对"
_BUCKETS = [
    ("big_up", "大涨", 8.0, float("inf")),
    ("up", "小涨", 2.0, 8.0),
    ("flat", "震荡", -2.0, 2.0),
    ("down", "小跌", -8.0, -2.0),
    ("big_down", "大跌", float("-inf"), -8.0),
]
_BUCKET_KEYS = [b[0] for b in _BUCKETS]
_BUCKET_LABEL = {b[0]: b[1] for b in _BUCKETS}


def bucket_of(ret_pct: float) -> str:
    """持有收益(%) → 分档 key。边界采用 [lo, hi)（大涨取 >=8，大跌取 <-8）。"""
    for key, _label, lo, hi in _BUCKETS:
        if lo <= ret_pct < hi:
            return key
    return "big_up" if ret_pct >= 8.0 else "big_down"


def buckets_meta() -> list[dict]:
    """供前端渲染 5 个选项按钮。"""
    return [{"key": k, "label": lab} for k, lab in _BUCKET_LABEL.items()]


def _direction(key: str) -> str:
    """档位 → 大方向(涨/平/跌)，用于"方向是否判对"统计。"""
    return {"big_up": "up", "up": "up", "flat": "flat", "down": "down", "big_down": "down"}[key]


def score(pred: str, actual: str) -> dict:
    """评分：完全命中=1·相邻档=0.5·否则 0；另记大方向是否判对。"""
    if pred not in _BUCKET_KEYS:
        pred = "flat"
    pi, ai = _BUCKET_KEYS.index(pred), _BUCKET_KEYS.index(actual)
    gap = abs(pi - ai)
    pts = 1.0 if gap == 0 else (0.5 if gap == 1 else 0.0)
    return {"points": pts, "exact": gap == 0, "near": gap == 1,
            "direction_right": _direction(pred) == _direction(actual),
            "pred_label": _BUCKET_LABEL[pred], "actual_label": _BUCKET_LABEL[actual]}


def split_at(kline: pd.DataFrame, i: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """在下标 i(=T0) 处切：hist=bar[..i](含 T0·题面可见) / future=bar[i+1..](答案·不可见)。

    这是"零未来泄漏"的唯一切口——题面只许用 hist，future 只许进答案。
    """
    hist = kline.iloc[: i + 1].reset_index(drop=True)
    future = kline.iloc[i + 1:].reset_index(drop=True)
    return hist, future


def _ma(close: pd.Series, n: int) -> float | None:
    if len(close) < n:
        return None
    return round(float(close.iloc[-n:].mean()), 2)


def position_metrics(hist: pd.DataFrame, idx_hist: pd.DataFrame | None) -> dict:
    """T0 个股位置（全部只用 hist·零泄漏）：乖离/距高低/量能/相对大盘强弱。"""
    close = hist["close"].astype(float)
    c0 = float(close.iloc[-1])
    ma20 = _ma(close, 20)
    hi60 = round(float(close.iloc[-60:].max()), 2) if len(close) >= 60 else None
    lo60 = round(float(close.iloc[-60:].min()), 2) if len(close) >= 60 else None
    vol = hist["vol"].astype(float)
    vr = (round(float(vol.iloc[-5:].mean()) / float(vol.iloc[-25:-5].mean()), 2)
          if len(vol) >= 25 and float(vol.iloc[-25:-5].mean()) > 0 else None)
    r20 = round((c0 / float(close.iloc[-21]) - 1) * 100, 2) if len(close) >= 21 else None
    rel = None
    if r20 is not None and idx_hist is not None and len(idx_hist) >= 21:
        ic = idx_hist["close"].astype(float)
        ir20 = (float(ic.iloc[-1]) / float(ic.iloc[-21]) - 1) * 100
        rel = round(r20 - ir20, 2)                # 个股近20日 − 上证近20日 = 相对强弱
    return {
        "price": round(c0, 2),
        "ma20": ma20,
        "bias20": round((c0 / ma20 - 1) * 100, 2) if ma20 else None,
        "dist_high60": round((c0 / hi60 - 1) * 100, 2) if hi60 else None,
        "dist_low60": round((c0 / lo60 - 1) * 100, 2) if lo60 else None,
        "vol_ratio5": vr,
        "ret20": r20,
        "rel_strength20": rel,
    }


def classify_setup(hist: pd.DataFrame) -> tuple[str, str]:
    """T0 形态归类（只用 hist）·用于"弱项强化/分形态胜率"统计。返回 (tag, 中文标签)。"""
    close = hist["close"].astype(float)
    c0 = float(close.iloc[-1])
    last_pct = float(hist["pct_chg"].iloc[-1]) if "pct_chg" in hist.columns else 0.0
    ma20 = _ma(close, 20)
    hi20 = float(close.iloc[-20:].max()) if len(close) >= 20 else c0
    r10 = (c0 / float(close.iloc[-11]) - 1) * 100 if len(close) >= 11 else 0.0
    prev = float(close.iloc[-2]) if len(close) >= 2 else c0
    if last_pct >= 9.7:
        return "limit_up", "涨停/连板"
    if c0 >= hi20 * 0.999 and r10 > 3:
        return "breakout", "突破新高"
    if ma20 and prev >= ma20 > c0:
        return "break_ma20", "跌破MA20"
    if ma20 and abs(c0 / ma20 - 1) <= 0.02 and c0 >= ma20:
        return "pullback_ma20", "回踩MA20"
    if r10 <= -15:
        return "oversold", "超跌"
    if ma20 and c0 > ma20 and r10 > 0:
        return "uptrend", "均线上行"
    if ma20 and c0 < ma20:
        return "weak", "均线下方"
    return "range", "震荡盘整"


def market_state(idx_hist: pd.DataFrame | None) -> str:
    """大盘粗状态（只用上证 hist·题面背景）：强/震/弱，按近20日涨幅+是否站 MA20。"""
    if idx_hist is None or len(idx_hist) < 21:
        return "未知"
    ic = idx_hist["close"].astype(float)
    r20 = float(ic.iloc[-1]) / float(ic.iloc[-21]) - 1
    ma20 = float(ic.iloc[-20:].mean())
    above = float(ic.iloc[-1]) >= ma20
    if r20 >= 0.03 and above:
        return "强"
    if r20 <= -0.03 and not above:
        return "弱"
    return "震荡"


def _chart(df: pd.DataFrame) -> dict:
    """日线 DataFrame → ECharts 友好结构（蜡烛 [开,收,低,高] + 量 + 日期）。"""
    o, c, l, h = (df[x].astype(float).round(2).tolist() for x in ("open", "close", "low", "high"))
    return {"dates": df["trade_date"].astype(str).tolist(),
            "candle": [[o[i], c[i], l[i], h[i]] for i in range(len(df))],
            "vol": df["vol"].astype(float).round(0).tolist(),
            "close": c}


# ── 抽样编排（连网·取数） ─────────────────────────────────────────────
_SH, _CYB = "000001.SH", "399006.SZ"
_REVEAL_MAX = 10                     # 揭晓最多展示 T+10
_MIN_LOOKBACK = DEFAULT_HIST         # T0 前至少 120 根展示（MA60 起始处自然留空·不补 pre-924）
_START_924 = "20240924"              # 只用 924 政策转向之后的数据（A股风格前后迥异，不混）
_PRIMARY_W = 0.75                    # 主：AI/科技 抽中权重；其余给次（金融/军工/航空）

# 主（AI/科技·硬件/软件/材料/设备·沾边即可）→ 命中这些关键词的同花顺概念，其成分股入主池
_PRIMARY_KW = (
    "AI", "AIGC", "算力", "智算", "大模型", "多模态", "CPO", "光模", "光通信", "光器件",
    "PCB", "铜连接", "液冷", "半导体", "芯片", "存储", "MCU", "GPU", "封装", "晶圆", "光刻", "EDA",
    "消费电子", "面板", "MiniLED", "OLED", "摄像", "声学", "连接器", "被动元件", "MLCC", "载板",
    "服务器", "数据中心", "交换机", "机器人", "人形", "减速器",
    "软件", "信创", "鸿蒙", "操作系统", "数据要素", "云计算", "网络安全", "工业软件", "算法",
    "智能驾驶", "自动驾驶", "智能座舱", "氟", "含氟", "电子化学", "光刻胶", "湿电子", "靶材", "6G",
)
# 次（金融/证券/军工/航空·为辅）：概念名 + 申万行业 双兜底
_SECONDARY_KW = ("证券", "券商", "保险", "银行", "军工", "航空", "航天", "国防", "兵装", "大飞机", "低空")
_SECONDARY_IND = ("证券", "保险", "银行", "金融", "军工", "航空", "航天", "国防", "兵装", "船舶")

_UNIVERSE: list[tuple[str, str, str]] = []   # 缓存 [(ts_code, name, industry)]
_POOLS: dict = {}                            # 缓存 {primary:[...], secondary:[...]}（按概念加权抽样）


def _universe(provider) -> list[tuple[str, str, str]]:
    """可抽样股票池（主板/创业板/科创·非ST·缓存）。北交所/退市/ST 排除。"""
    global _UNIVERSE
    if _UNIVERSE:
        return _UNIVERSE
    df = provider.get_stock_basic()
    out = []
    for _i, r in df.iterrows():
        code, name = str(r.get("ts_code", "")), str(r.get("name", ""))
        if not code or code.endswith(".BJ"):
            continue
        if any(x in name for x in ("ST", "*", "退")):
            continue
        if code[:2] not in ("60", "68", "00", "30"):
            continue
        out.append((code, name, str(r.get("industry", "") or "")))
    _UNIVERSE = out
    return out


def _weighted_pools(provider) -> dict:
    """按同花顺概念名把股票池分主(AI/科技)/次(金融军工航空)；冷门不入池。缓存。"""
    if _POOLS:
        return _POOLS
    uni = _universe(provider)
    by_code = {c: (c, n, i) for c, n, i in uni}
    prim, sec = set(), set()
    try:
        from app.strategy.realtime_hub import concept_map
        for cname, members in (concept_map() or {}).items():
            if any(k in cname for k in _PRIMARY_KW):
                prim.update(members)
            elif any(k in cname for k in _SECONDARY_KW):
                sec.update(members)
    except Exception as e:
        logger.warning("[盘感] 概念分池失败，退化为申万行业兜底: %s", e)
    for c, _n, ind in uni:                       # 申万行业兜底金融/军工/航空
        if any(k in (ind or "") for k in _SECONDARY_IND):
            sec.add(c)
    primary = [by_code[c] for c in prim if c in by_code]
    secondary = [by_code[c] for c in sec if c in by_code and c not in prim]   # 主优先·不重叠
    _POOLS.update(primary=primary, secondary=secondary)
    logger.info("[盘感] 分池：主(AI/科技)%d 只 · 次(金融/军工/航空)%d 只", len(primary), len(secondary))
    return _POOLS


def _pick_weighted(provider) -> tuple[str, str, str]:
    """加权抽一只：主池 ~70% / 次池 ~30%；池空则兜底全市场。"""
    p = _weighted_pools(provider)
    prim, sec = p.get("primary") or [], p.get("secondary") or []
    if prim and (random.random() < _PRIMARY_W or not sec):
        return random.choice(prim)
    if sec:
        return random.choice(sec)
    return random.choice(_universe(provider))


def build_quiz(provider=None, *, code: str | None = None,
               hist_days: int = DEFAULT_HIST, fwd: int = DEFAULT_FWD) -> dict:
    """抽一局：选股+随机 T0 → 题面(截至T0·零泄漏) + 答案(T+1..T+N·隐藏)。

    Returns dict: {ok, question:{...}, answer:{...}}；失败 {ok:False, msg}。
    """
    if provider is None:
        from app.data.composite_provider import CompositeProvider
        provider = CompositeProvider()
    end = datetime.date.today().strftime("%Y%m%d")
    start = _START_924                              # 只用 924 之后（风格一致）

    pool = _universe(provider)
    for _try in range(8):                          # 选股+取数可能落空，重试几次
        if code:
            ts = code
            name = next((n for c, n, _i in pool if c == code), code[:6])
            industry = next((i for c, _n, i in pool if c == code), "")
        else:
            ts, name, industry = _pick_weighted(provider)   # AI/科技为主·金融军工航空为辅·冷门剔除
        kl = load_kline(ts, start, end, provider, adj="qfq")
        if kl is None or len(kl) < _MIN_LOOKBACK + _REVEAL_MAX + 2:
            if code:
                return {"ok": False, "msg": "该股 924 后数据不足以出题"}
            continue
        i = random.randint(_MIN_LOOKBACK, len(kl) - _REVEAL_MAX - 1)
        hist, future = split_at(kl, i)
        t0 = str(hist["trade_date"].iloc[-1])
        show = hist.tail(hist_days).reset_index(drop=True)
        show_start = str(show["trade_date"].iloc[0])
        fut_show = future.head(_REVEAL_MAX).reset_index(drop=True)
        reveal_end = str(fut_show["trade_date"].iloc[-1])
        # 指数取 [展示起点, 揭晓终点] 整段再按 T0 切：hist 进题面(背景)，future 进答案(揭晓后续·与K线同期)
        idx_sh, idx_sh_fut = _split_idx(_index_range(provider, _SH, show_start, reveal_end), t0)
        idx_cyb, idx_cyb_fut = _split_idx(_index_range(provider, _CYB, show_start, reveal_end), t0)

        c0 = float(hist["close"].iloc[-1])
        fwd_close = future["close"].astype(float).tolist()
        rets = {h: round((fwd_close[h - 1] / c0 - 1) * 100, 2)
                for h in (1, 3, 5, fwd, 10) if h <= len(fwd_close)}
        actual_bucket = bucket_of(rets.get(fwd, rets.get(5, 0.0)))
        setup_tag, setup_label = classify_setup(hist)
        state = market_state(idx_sh)

        question = {
            "stock": _chart(show),
            "ma": _ma_aligned(hist["close"].astype(float), len(show)),
            "index_sh": _chart(idx_sh) if idx_sh is not None and not idx_sh.empty else None,
            "index_cyb": _chart(idx_cyb) if idx_cyb is not None and not idx_cyb.empty else None,
            "industry": industry,
            "market_state": state,
            "position": position_metrics(hist, idx_sh),
            "setup_label": setup_label,
            "fwd": fwd,
            "buckets": buckets_meta(),
        }
        answer = {
            "ts_code": ts, "name": name, "t0": t0,
            "rets": rets, "bucket": actual_bucket,
            "setup_tag": setup_tag, "setup_label": setup_label, "market_state": state,
            "future": _chart(fut_show),
            "index_sh_future": _chart(idx_sh_fut) if idx_sh_fut is not None and not idx_sh_fut.empty else None,
            "index_cyb_future": _chart(idx_cyb_fut) if idx_cyb_fut is not None and not idx_cyb_fut.empty else None,
        }
        return {"ok": True, "question": question, "answer": answer}
    return {"ok": False, "msg": "多次抽样取数失败，请重试"}


def _index_range(provider, idx_code: str, start: str, end: str):
    """指数日线区间（容错·失败回 None）。"""
    try:
        df = provider.get_index_daily_range(idx_code, start, end)
        return df.sort_values("trade_date").reset_index(drop=True) if df is not None and not df.empty else None
    except Exception:
        return None


def _split_idx(df, t0: str):
    """指数整段按 T0 切 → (hist ≤T0·题面背景, future >T0·揭晓后续)。None 安全。"""
    if df is None or df.empty:
        return None, None
    d = df["trade_date"].astype(str)
    hist = df[d <= t0].reset_index(drop=True)
    fut = df[d > t0].reset_index(drop=True)
    return (hist if not hist.empty else None), (fut if not fut.empty else None)


def _ma_aligned(close: pd.Series, take: int) -> dict:
    """各周期均线·只取最后 take 根(对齐题面展示窗口)·不足处为 None。只用 ≤T0 数据。"""
    out = {}
    for n in (5, 10, 20, 60):
        s = close.rolling(n).mean().round(2).tolist()[-take:]
        out[n] = [None if pd.isna(v) else v for v in s]
    return out
