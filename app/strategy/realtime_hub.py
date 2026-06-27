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
_HISTORY: deque = deque(maxlen=16)        # [(epoch, {code: price})]·约采样6-8分钟
_STALE_SEC = 15                           # 超过此秒数未更新 → 视为非实时


def ensure_started() -> bool:
    """幂等启动全推客户端（依赖 .env 的 fullpush_*）。未配置则跳过。"""
    global _CLIENT
    from app.config import get_settings
    s = get_settings()
    if not (s.fullpush_host and s.fullpush_port and s.fullpush_token):
        logger.info("[实时枢纽] 未配置 fullpush_*，跳过全推接入")
        return False
    with _LOCK:
        if _CLIENT is None:
            _CLIENT = FullPushClient.from_settings(_SNAP)
        if not _CLIENT.running:
            _CLIENT.start()
    return True


def snapshot() -> MarketSnapshot:
    return _SNAP


def is_live() -> bool:
    """全推是否在实时供数（用于决定走全推还是回退新浪）。"""
    return not _SNAP.is_stale(_STALE_SEC)


def status() -> dict:
    return {"running": bool(_CLIENT and _CLIENT.running), "live": is_live(),
            "count": _SNAP.count(), "as_of": _as_of_str()}


def _as_of_str() -> str:
    t = _SNAP.updated_at
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else ""


def record_history() -> None:
    """采样当前价照，供急拉/涨速计算（由扫描线程定期调用）。"""
    prices = _SNAP.prices()
    if prices:
        _HISTORY.append((time.time(), prices))


def past_prices(minutes: float = 5.0) -> dict:
    """约 minutes 分钟前的价照；不足则取最早一帧。"""
    if not _HISTORY:
        return {}
    cutoff = time.time() - minutes * 60
    older = [snap for t, snap in _HISTORY if t <= cutoff]
    return older[-1] if older else _HISTORY[0][1]


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
    base = {"ok": True, "live": is_live(), "as_of": _as_of_str(), "count": int(len(df))}
    if df.empty:
        base.update({"msg": "全推未连接（休市或未开盘），开盘自动接入"})
        return base
    from app.strategy.realtime_fund import fund_ranking, sector_fund
    base["fund_ranking"] = fund_ranking(df, top=15)
    imap = _industry_map()
    base["sector_fund"] = sector_fund(df, imap, top=12)
    base.update(_radar_block(df, imap))
    base["surge"] = _velocity_block()
    base["holdings"] = _holdings_block()
    return base


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


def _holdings_block() -> list[dict]:
    """持仓实时体检（读自选库 is_holding=1）。"""
    from app.strategy import db
    from app.strategy.realtime_fund import holding_health, outer_ratio
    out = []
    for w in db.get_watchlist():
        if not w.get("is_holding"):
            continue
        q = _SNAP.get(w["ts_code"])
        if not q:
            continue
        label, reason = holding_health(q, w.get("stop_loss"))
        out.append({"ts_code": w["ts_code"], "name": q.get("name", ""),
                    "pct_chg": round(float(q.get("pct_chg") or 0), 2),
                    "vol_ratio": round(float(q.get("vol_ratio") or 0), 2),
                    "outer_ratio": outer_ratio(q.get("inner") or 0, q.get("outer") or 0),
                    "label": label, "reason": reason})
    return out
