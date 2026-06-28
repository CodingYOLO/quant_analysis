"""实时行情运行时枢纽：进程内单例的全推连接 + 快照 + 看板聚合。

Web 进程启动时 ensure_started()，FullPushClient 后台线程持续把全推写入快照；
页面/扫描器只读快照。生产端仅交易时间开放，客户端断线指数退避重连——
休市连不上不报错，开盘自动接上。快照陈旧（is_stale）时上层可回退新浪。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from app.data.fullpush.client import FullPushClient
from app.data.fullpush.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

_SNAP = MarketSnapshot()
_CLIENT: FullPushClient | None = None
_LOCK = threading.Lock()
_IND_MAP: dict | None = None
_CONCEPT_MAP: dict | None = None
_TECH_MAP: dict | None = None
_TECH_MAP_KEY = ""                          # 已加载的因子表文件路径（变了就重载）
_TECH_COLS = ["ma_bull_full", "above_ma20", "above_ma60", "above_ma120", "above_ma250",
              "ma20_up", "stable_above_ma20", "rps120", "pat_breakout_high_20", "vol5_vol20",
              "ma20", "ma60", "high20", "low20", "close",   # v15: 关键位数值(供实时突破/破位)
              "consec_limit_now", "macd_gold"]               # v16: 昨收当前连板 + MACD金叉
_TAIL_BASE: dict = {}                      # 尾盘14:30基准 {code:{price,net}}
_TAIL_DATE: str = ""
_HISTORY: deque = deque(maxlen=16)        # [(epoch, {code: price})]·约采样6-8分钟
_NET_HISTORY: deque = deque(maxlen=20)     # [(epoch, {code: 主动净买亿})]·资金持续/脉冲判定
_STALE_SEC = 15                           # 超过此秒数未更新 → 视为非实时


# 公开测试端点（回放数据·休市预览/演示用，非生产）
_DEMO_ENDPOINT = ("test.chagubang.com", 48988, "mushuju")


def ensure_started() -> bool:
    """幂等启动全推客户端。demo 开关→公开测试端点；否则→.env 生产授权。"""
    global _CLIENT
    from app.config import get_settings
    s = get_settings()
    if s.fullpush_demo:
        host, port, token = _DEMO_ENDPOINT
        logger.info("[实时枢纽] 演示模式：接测试端点（回放数据）")
    elif s.fullpush_host and s.fullpush_port and s.fullpush_token:
        host, port, token = s.fullpush_host, s.fullpush_port, s.fullpush_token
    else:
        logger.info("[实时枢纽] 未配置 fullpush_*，跳过全推接入")
        return False
    with _LOCK:
        if _CLIENT is None:
            _CLIENT = FullPushClient(host, port, token, _SNAP)
        if not _CLIENT.running:
            _CLIENT.start()
    return True


def snapshot() -> MarketSnapshot:
    return _SNAP


def is_live() -> bool:
    """**全推**是否在实时供数（新浪兜底填的不算）。决定走全推信号还是降级。"""
    return not _SNAP.fullpush_stale(_STALE_SEC)


def data_fresh() -> bool:
    """快照是否有新数据（任意来源·全推或新浪兜底）。"""
    return not _SNAP.is_stale(_STALE_SEC)


def fallback_fill_from_sina(top_active: int = 800) -> int:
    """全推断流时·用新浪批量报价填充快照（保命：涨跌幅维度·无内外盘）。返回填充只数。"""
    try:
        from app.data.composite_provider import CompositeProvider
        from app.strategy.market_radar import _active_universe, _chunked_quotes
        provider = CompositeProvider()
        df = _chunked_quotes(provider, _active_universe(provider, top_active))
        if df is None or df.empty:
            return 0
        rows = df.to_dict("records")
        _SNAP.update_external(rows)
        return len(rows)
    except Exception as e:
        logger.warning("[实时枢纽] 新浪兜底填充失败：%s", e)
        return 0


def status() -> dict:
    return {"running": bool(_CLIENT and _CLIENT.running), "live": is_live(),
            "source": _SNAP.source, "count": _SNAP.count(), "as_of": _as_of_str()}


def _as_of_str() -> str:
    t = _SNAP.updated_at
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else ""


def record_history() -> None:
    """采样当前价照，供急拉/涨速计算（由扫描线程定期调用）。"""
    prices = _SNAP.prices()
    if prices:
        _HISTORY.append((time.time(), prices))


def record_net_history() -> None:
    """采样各股当日累计主动净买，供资金持续/脉冲判定（扫描线程定期调用）。"""
    nets = _SNAP.net_amounts()
    if nets:
        _NET_HISTORY.append((time.time(), nets))


def net_series(code: str) -> list:
    """某股最近若干采样的主动净买序列（升序·最早→最新）。"""
    return [nets[code] for _, nets in _NET_HISTORY if code in nets]


def past_prices(minutes: float = 5.0) -> dict:
    """约 minutes 分钟前的价照；不足则取最早一帧。"""
    if not _HISTORY:
        return {}
    cutoff = time.time() - minutes * 60
    older = [snap for t, snap in _HISTORY if t <= cutoff]
    return older[-1] if older else _HISTORY[0][1]


def industry_map() -> dict:
    """对外暴露行业映射（扫描器/页面共用，避免各自重复加载）。"""
    return _industry_map()


def concept_map() -> dict:
    """{概念:[成分 ts_code]}（进程内缓存；底层周缓存）。题材发酵识别用。"""
    global _CONCEPT_MAP
    if _CONCEPT_MAP is None:
        try:
            from app.data.composite_provider import CompositeProvider
            from app.factors.theme_wide import concept_members_map
            _CONCEPT_MAP = concept_members_map(CompositeProvider())
        except Exception as e:
            logger.warning("[实时枢纽] 概念成分加载失败：%s", e)
            _CONCEPT_MAP = {}
    return _CONCEPT_MAP


def tech_map() -> dict:
    """{ts_code: 技术姿态dict + 关键位数值}（读最新因子表·**检测到新表自动重载**·避免周中用旧均线）。"""
    global _TECH_MAP, _TECH_MAP_KEY
    try:
        import glob

        from app.config import get_settings
        from app.strategy.screener import _FACTOR_TABLE_VERSION
        files = sorted(glob.glob(str(get_settings().cache_dir / "factor_table"
                                     / f"*_{_FACTOR_TABLE_VERSION}.parquet")))
        key = files[-1] if files else ""
        if _TECH_MAP is not None and key == _TECH_MAP_KEY:
            return _TECH_MAP                              # 文件未变·用缓存
        if not key:
            _TECH_MAP, _TECH_MAP_KEY = {}, ""
            return _TECH_MAP
        import pandas as pd
        df = pd.read_parquet(key)
        cols = [c for c in _TECH_COLS if c in df.columns]
        _TECH_MAP = {r["ts_code"]: {c: r.get(c) for c in cols}
                     for r in df[["ts_code"] + cols].to_dict("records")}
        _TECH_MAP_KEY = key
    except Exception as e:
        logger.warning("[实时枢纽] 技术姿态加载失败：%s", e)
        if _TECH_MAP is None:
            _TECH_MAP = {}
    return _TECH_MAP


def is_tail_session(now: float | None = None) -> bool:
    """是否尾盘时段（14:30-15:00）。"""
    hm = time.strftime("%H%M", time.localtime(now)) if now else time.strftime("%H%M")
    return "1430" <= hm <= "1500"


def market_session(now: float | None = None) -> str:
    """交易时段（含集合竞价）：
    'auction'(9:15-9:25 开盘集合竞价) / 'pre_open'(9:25-9:30 过渡) /
    'continuous'(连续竞价 9:30-11:30 / 13:00-15:00) / 'closed'(休市)。
    """
    import datetime
    dt = datetime.datetime.fromtimestamp(now) if now else datetime.datetime.now()
    if dt.weekday() >= 5:
        return "closed"
    hm = dt.strftime("%H%M")
    if "0915" <= hm < "0925":
        return "auction"
    if "0925" <= hm < "0930":
        return "pre_open"
    if ("0930" <= hm <= "1130") or ("1300" <= hm <= "1500"):
        return "continuous"
    return "closed"


def watch_meta() -> dict:
    """自选/持仓元信息 {ts_code: {name, is_holding, stop_loss}}（集合竞价/盯盘异动用）。"""
    from app.strategy import db
    return {w["ts_code"]: {"name": w.get("name", ""), "is_holding": bool(w.get("is_holding")),
                           "stop_loss": w.get("stop_loss")} for w in db.get_watchlist()}


def record_tail_baseline(rows: list[dict]) -> None:
    """进入尾盘首次记录 14:30 基准（幂等·按交易日自动重置）。"""
    global _TAIL_BASE, _TAIL_DATE
    today = time.strftime("%Y%m%d")
    if _TAIL_DATE != today:
        _TAIL_BASE, _TAIL_DATE = {}, today
    if not _TAIL_BASE and rows:
        from app.strategy.realtime_fund import tail_baseline_of
        _TAIL_BASE = tail_baseline_of(rows)


def tail_baseline() -> dict:
    """当日尾盘基准（跨日自动失效）。"""
    return _TAIL_BASE if _TAIL_DATE == time.strftime("%Y%m%d") else {}


def _industry_map() -> dict:
    """申万二级行业映射（进程内缓存一次；失败返回空 → 板块聚合降级）。"""
    global _IND_MAP
    if _IND_MAP is None:
        try:
            from app.data.composite_provider import CompositeProvider
            sb = CompositeProvider().get_stock_basic()
            _IND_MAP = dict(zip(sb["ts_code"], sb["industry"].fillna("")))
        except Exception as e:
            logger.warning("[实时枢纽] 行业映射加载失败：%s", e)
            _IND_MAP = {}
    return _IND_MAP


def build_board() -> dict:
    """汇总实时看板数据（资金榜/板块/大盘温度/急拉/持仓体检）。"""
    df = _SNAP.to_df()
    base = {"ok": True, "live": is_live(), "session": market_session(),
            "as_of": _as_of_str(), "count": int(len(df))}
    base["watch"] = _watch_block()                        # 自选/持仓·休市也回(空快照时为[])
    if df.empty:
        base.update({"msg": "全推未连接（休市或未开盘），开盘自动接入"})
        return base
    imap = _industry_map()
    if base["session"] in ("auction", "pre_open"):        # 集合竞价：全市场价格类信号(量资金信号开盘后才有意义)
        return _auction_board(base, df, imap)
    from app.strategy.realtime_fund import (altitude_risk, fund_ranking, sector_board, tech_context)
    fr = fund_ranking(df, top=15)
    tm = tech_map()
    pcmap = dict(zip(df["ts_code"], df["prev_close"]))    # 昨收(尺度对齐校验用)
    for r in fr:                                          # 资金榜补实时技术位 + 高位风险
        price, prev, t = r.get("price"), pcmap.get(r["ts_code"]), tm.get(r["ts_code"])
        r["tech"] = "·".join(x for x in (tech_context(price, prev, t),
                                         altitude_risk(price or 0, prev or 0, t)) if x)
    base["fund_ranking"] = fr
    full = sector_board(df, imap)                          # 全部板块·含龙头
    base["sectors"] = full[:12]                            # 资金涌入榜(机会)
    base["sectors_out"] = [s for s in reversed(full) if s["net_yi"] < 0][:6]   # 资金撤离(风险)
    records = df.to_dict("records")                       # 转一次·多块复用
    base.update(_radar_block(df, imap))
    base["sentiment"] = _sentiment_block(records, tm)     # 情绪温度计(连板梯队/晋级率/炸板率)
    base["themes"] = _theme_block(records)
    base["tail"] = _tail_block(records, imap)
    base["flash"] = _flash_block(records)
    base["surge"] = _velocity_block()
    from app.strategy.realtime_fund import market_brief
    secs = base.get("sectors") or []
    top_in = secs[0]["industry"] if secs and secs[0].get("net_yi", 0) > 0 else ""
    base["brief"] = market_brief(base.get("sentiment", {}), base.get("breadth", {}), top_in)
    return base


def _auction_board(base: dict, df, imap: dict) -> dict:
    """集合竞价看板（9:15-9:30）：全市场纯价格口径——板块竞价强弱 / 高开低开排行 / 竞价情绪 / 涨跌家数。

    内外盘资金/急拉/闪崩等量资金信号竞价时无意义，开盘后(continuous)才出，故此处不算。
    """
    from app.strategy.realtime_fund import auction_movers, auction_sector_strength, auction_sentiment
    records = df.to_dict("records")
    base["auction"] = {
        "sectors": auction_sector_strength(records, imap, top=10),    # 板块竞价方向
        "movers": auction_movers(records, imap, top=10),              # 高开/低开排行
        "sentiment": auction_sentiment(records),                      # 全市场竞价情绪
    }
    base.update(_radar_block(df, imap))                               # breadth(涨跌家数/竞价涨停·价格口径有效)
    return base


def _tail_block(records: list[dict], imap: dict) -> dict:
    """尾盘异动块（仅尾盘时段且已记录14:30基准时填充）。"""
    if not is_tail_session() or not tail_baseline():
        return {}
    from app.strategy.realtime_fund import tail_movers, tail_sector_flow
    tb = tail_baseline()
    mv = tail_movers(records, tb)
    return {"sectors": tail_sector_flow(records, tb, imap, top=8),
            "ups": [m for m in mv if m["kind"] == "up"][:8],
            "downs": [m for m in mv if m["kind"] == "down"][:8]}


def _flash_block(records: list[dict]) -> list[dict]:
    """急跌/闪崩监控（3分钟瞬时跌速 + 放量 + 内盘主动砸）。"""
    from app.strategy.realtime_fund import detect_flash_crashes
    return detect_flash_crashes(records, past_prices(3.0))[:8]


def _sentiment_block(records: list[dict], tm: dict) -> dict:
    """情绪温度计（连板梯队/空间板/晋级率/炸板率·消费昨收当前连板）。"""
    from app.strategy.realtime_fund import sentiment_thermometer
    consec = {c: (t.get("consec_limit_now") or 0) for c, t in tm.items()}
    return sentiment_thermometer(records, consec)


def _theme_block(records: list[dict]) -> list[dict]:
    """题材发酵榜（Tushare概念成分 × 全推实时涨幅）。"""
    from app.strategy.realtime_fund import detect_theme_fermentation
    try:
        return detect_theme_fermentation(records, concept_map())[:8]
    except Exception as e:
        logger.warning("[实时枢纽] 题材发酵失败：%s", e)
        return []


def _radar_block(df, imap: dict) -> dict:
    """复用市场雷达聚合：大盘温度 + 板块热力。"""
    try:
        from app.nodes.quick_report import _board_limit_pct
        from app.strategy.market_radar import _aggregate_radar
        r = _aggregate_radar(df, imap, _board_limit_pct)
        return {"breadth": r.get("breadth", {}), "hot_sectors": r.get("hot_sectors", []),
                "weak_sectors": r.get("weak_sectors", [])}
    except Exception as e:
        logger.warning("[实时枢纽] 雷达聚合失败：%s", e)
        return {"breadth": {}, "hot_sectors": [], "weak_sectors": []}


def _velocity_block() -> list[dict]:
    """急拉榜：现价 vs 约5分钟前。名称从快照补。"""
    from app.strategy.realtime_fund import velocity_events
    ev = velocity_events(_SNAP.prices(), past_prices(5.0), min_move=1.5)[:10]
    for e in ev:
        q = _SNAP.get(e["ts_code"])
        e["name"] = (q or {}).get("name", e["ts_code"])
    return ev


def _watch_block() -> list[dict]:
    """自选/持仓实时盯盘（读自选库全部·持仓在前·组内按涨幅排）。

    之前只回 is_holding=1，用户的自选(is_holding=0)全被漏掉→看板"没同步自选"。现自选/持仓都回，
    用 is_holding 区分；持仓带止损做体检，自选同样给实时量价+体检读数。
    """
    from app.strategy import db
    from app.strategy.realtime_fund import holding_health, outer_ratio
    out = []
    for w in db.get_watchlist():
        q = _SNAP.get(w["ts_code"])
        if not q:
            continue
        label, reason = holding_health(q, w.get("stop_loss"))
        out.append({"ts_code": w["ts_code"], "name": q.get("name", ""),
                    "is_holding": bool(w.get("is_holding")),
                    "pct_chg": round(float(q.get("pct_chg") or 0), 2),
                    "vol_ratio": round(float(q.get("vol_ratio") or 0), 2),
                    "outer_ratio": outer_ratio(q.get("inner") or 0, q.get("outer") or 0),
                    "label": label, "reason": reason})
    out.sort(key=lambda x: (not x["is_holding"], -x["pct_chg"]))   # 持仓在前·组内涨幅降序
    return out
