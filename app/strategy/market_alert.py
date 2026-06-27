"""全市场盘中提醒：定时扫全市场雷达 → 板块弱转强 / 涨停潮 / 强势热点 / 集合竞价强势 → 推 Bark。

不用加自选——直接帮你**观察整个市场**。守纪律：只报事实·不下单·不催追高；
每个事件**一天只推一次**（去重）。复用 market_radar（全市场新浪扫描·~20s·故由 cron 每15分钟跑）。
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from app.config import get_settings
from app.strategy.watch_alert import is_market_hours

logger = logging.getLogger(__name__)

_WEAK_OPEN = -0.8       # 开盘板块均涨幅 ≤ 此 = 弱
_STRONG_NOW = 0.6       # 现板块均涨幅 ≥ 此 = 强（弱转强阈值）
_HOT_PCT = 2.5          # 强势热点板块阈值（均涨）
_LIMIT_LEVELS = (15, 30, 50)   # 涨停潮档位（活跃股池口径·比全市场口径低·活跃龙头才是情绪核心）
_ACTIVE_TOP = 800       # 只扫成交额前 N 的活跃股池（快·覆盖热点龙头）→ 可每 2-3 分钟跑


def detect_market_events(radar: dict, open_sector_pct: dict, limit_max: int,
                         first_scan: bool, now_hm: str) -> tuple[list, dict, int]:
    """纯函数：雷达 + 当日基准 → 事件 [(key, title, body)]、当前板块均涨幅、当前涨停数。"""
    hot = radar.get("hot_sectors") or []
    breadth = radar.get("breadth") or {}
    cur_pct = {h["industry"]: h["avg_pct"] for h in hot}
    for w in (radar.get("weak_sectors") or []):
        cur_pct.setdefault(w["industry"], w["avg_pct"])

    events: list[tuple[str, str, str]] = []
    # 1) 集合竞价/开盘·强势板块（仅当日首扫且在开盘附近）
    if first_scan and now_hm <= "0935" and hot:
        body = "\n".join(f"{h['industry']} {h['avg_pct']:+.1f}%（领涨 {h['leader']} {h['leader_pct']:+.1f}%）"
                         for h in hot[:3])
        events.append(("auction", "🌐 集合竞价/开盘·强势板块", body + "\n（盘前快照·仅供观察）"))
    # 2) 板块弱转强（开盘弱 → 现强）
    for h in hot:
        op = open_sector_pct.get(h["industry"])
        if op is not None and op <= _WEAK_OPEN and h["avg_pct"] >= _STRONG_NOW:
            events.append((f"flip_{h['industry']}", "🌐 板块弱转强",
                           f"{h['industry']} 开盘 {op:+.1f}% → 现 {h['avg_pct']:+.1f}%"
                           f"（领涨 {h['leader']} {h['leader_pct']:+.1f}%）"))
    # 3) 强势热点板块（均涨 ≥ 阈值·首次过线）
    for h in hot[:3]:
        if h["avg_pct"] >= _HOT_PCT:
            events.append((f"hot_{h['industry']}", "🌐 强势热点板块",
                           f"{h['industry']} {h['avg_pct']:+.1f}% 领涨"
                           f"（涨停{h['limit_up']}·领头 {h['leader']} {h['leader_pct']:+.1f}%）"))
    # 4) 涨停潮（过档·一次扫描只推【本次新过的最高档】，避免首扫一次性弹多条）
    lu = int(breadth.get("limit_up", 0) or 0)
    crossed = [lvl for lvl in _LIMIT_LEVELS if lu >= lvl > limit_max]
    if crossed:
        lvl = max(crossed)
        events.append((f"limit_{lvl}", "🌐 涨停潮",
                       f"活跃股涨停 {lu} 只（成交额前800口径·非全市场）·市场情绪升温"))
    return events, cur_pct, lu


# ── 当日状态（开盘基准 + 已推事件 + 涨停峰值）──────────────────────────────────
def _state_path(date: str) -> Path:
    d = get_settings().cache_dir / "market_alert"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{date}.json"


def _load_state(date: str) -> dict:
    p = _state_path(date)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"open_sector_pct": {}, "pushed": [], "limit_max": 0, "scans": 0}


def _save_state(date: str, st: dict) -> None:
    try:
        _state_path(date).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def scan_market_alerts(provider=None, push: bool = True, force: bool = False) -> list[dict]:
    """扫全市场雷达 → 推【今天未推过】的市场事件。返回本次新推列表。"""
    if not force and not is_market_hours():
        return []
    from app.strategy.market_radar import build_market_radar
    radar = build_market_radar(provider, top_active=_ACTIVE_TOP)   # 只扫活跃股池·快·可高频
    if not (radar.get("hot_sectors") or (radar.get("breadth") or {}).get("total")):
        return []

    date = datetime.date.today().strftime("%Y%m%d")
    st = _load_state(date)
    first_scan = st.get("scans", 0) == 0
    now_hm = datetime.datetime.now().strftime("%H%M")
    events, cur_pct, lu = detect_market_events(
        radar, st.get("open_sector_pct", {}), st.get("limit_max", 0), first_scan, now_hm)

    if first_scan:
        st["open_sector_pct"] = cur_pct                  # 记开盘基准（弱转强对比用）

    pushed = set(st.get("pushed", []))
    from app.notify.notifier import push_bark
    new: list[dict] = []
    for key, title, body in events:
        if key in pushed:
            continue
        if (not push) or push_bark(title, body, group="全市场"):
            pushed.add(key)
            new.append({"key": key, "title": title, "body": body})

    st["pushed"] = sorted(pushed)
    st["limit_max"] = max(st.get("limit_max", 0), lu)
    st["scans"] = st.get("scans", 0) + 1
    _save_state(date, st)
    return new
