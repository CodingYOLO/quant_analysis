"""盯盘实时提醒：交易时段定时扫描自选/持仓，命中触发（到买入价/破止损/异动）即推 Bark 到手机。

守 CLAUDE.md：**只报事实、不下单、不催追高**（"距目标差2.7%"而非"快买！"）。
- 价格类触发只用实时价 + 用户设的目标价/止损，轻量，1–3 分钟可扫一遍；
- 每个 (股票, 触发) **一天只推一次**（去重，避免每次扫描刷屏）；
- 仅交易时段扫描（盘后/周末不推）。
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def is_market_hours(now: datetime.datetime | None = None) -> bool:
    """A股交易时段（含开盘/收盘小缓冲；周末不算）。"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H%M")
    return ("0930" <= hm <= "1131") or ("1300" <= hm <= "1501")


def compute_triggers(row: dict) -> list[tuple[str, str]]:
    """价格类触发 [(key, 文案)]。纯函数：只用实时价 + 目标价/止损，便于单测。

    row: {name, price, pct_chg, target_price, stop_loss}
    """
    out: list[tuple[str, str]] = []
    p, tp, stop, pct = row.get("price"), row.get("target_price"), row.get("stop_loss"), row.get("pct_chg")
    if p and tp:
        gap = (p / tp - 1) * 100
        if p <= tp:
            out.append(("at_buy", f"✅ 已到买入区  目标{tp} · 现价{p}（低于目标{abs(gap):.1f}%）"))
        elif gap <= 3:
            out.append(("near_buy", f"🟢 逼近买入区  目标{tp} · 现价{p} · 还差{gap:.1f}%"))
    if p and stop and p <= stop:
        out.append(("break_stop", f"🛑 跌破止损位 {stop} · 现价{p}"))
    if pct is not None:
        if pct <= -7:
            out.append(("big_drop", f"⚠️ 当日大跌 {pct:+.1f}%"))
        elif pct >= 9.8:
            out.append(("near_limit", f"🔴 涨停/逼近涨停 {pct:+.1f}%"))
    return out


# ── 当日去重（每个 票:触发 一天只推一次）──────────────────────────────────────
def _dedup_path(date: str) -> Path:
    d = get_settings().cache_dir / "watch_alert"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{date}.json"


def _load_pushed(date: str) -> set[str]:
    p = _dedup_path(date)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def _save_pushed(date: str, pushed: set[str]) -> None:
    try:
        _dedup_path(date).write_text(json.dumps(sorted(pushed), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def scan_watch_alerts(provider=None, push: bool = True, force: bool = False) -> list[dict]:
    """扫描自选/持仓，对【今天未推过】的触发推 Bark。返回本次新推的提醒列表。

    Args:
        push:  False 时只算不推（测试/预览）。
        force: True 时忽略交易时段限制（手动测试用）。
    """
    if not force and not is_market_hours():
        return []
    if provider is None:
        from app.data.composite_provider import CompositeProvider
        provider = CompositeProvider()
    from app.strategy import db
    watch = db.get_watchlist()
    if not watch:
        return []

    qmap: dict[str, dict] = {}
    try:
        q = provider.get_realtime_quote([w["ts_code"] for w in watch])
        if q is not None and not q.empty:
            for _, r in q.iterrows():
                qmap[str(r["ts_code"])] = {"price": round(float(r["price"]), 2),
                                           "pct_chg": round(float(r["pct_chg"]), 2)}
    except Exception as e:
        logger.warning("[盯盘] 实时价获取失败: %s", e)
        return []

    date = datetime.date.today().strftime("%Y%m%d")
    pushed = _load_pushed(date)
    base = get_settings().web_base_url or ""
    new_alerts: list[dict] = []

    from app.notify.notifier import push_bark
    for w in watch:
        ts = w["ts_code"]
        q = qmap.get(ts)
        if not q:
            continue
        row = {"name": w.get("name") or ts, "price": q["price"], "pct_chg": q["pct_chg"],
               "target_price": w.get("target_price"), "stop_loss": w.get("stop_loss")}
        fresh = [(k, msg) for k, msg in compute_triggers(row) if f"{ts}:{k}" not in pushed]
        if not fresh:
            continue
        name, c6 = row["name"], ts.split(".")[0]
        head = f"{name}（{c6}）现价 {q['price']}（{q['pct_chg']:+.1f}%）"
        body = head + "\n" + "\n".join(m for _, m in fresh)
        url = f"{base}/stock?code={c6}" if base else ""
        ok = (not push) or push_bark(f"🛎️ {name}·盯盘提醒", body, url=url)
        if ok:
            for k, _ in fresh:
                pushed.add(f"{ts}:{k}")
            new_alerts.append({"ts_code": ts, "name": name, "triggers": [m for _, m in fresh]})

    _save_pushed(date, pushed)
    return new_alerts
