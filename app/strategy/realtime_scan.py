"""实时盯盘扫描器（进程内·喂自全推快照 → 推 Bark）。

只做全推独有信号，避免与现有新浪 cron（弱转强/涨停潮/集合竞价）重复推送：
  - 资金抢筹：外盘占比高 + 放量 + 上涨 + 主动净买达标
  - 急拉：约5分钟涨速达标
  - 持仓异动：你的持仓急跌/破位/资金转主动卖 → 提示「走一遍」冷静流程
每个事件当天只推一次（进程内去重）。后台线程每 ~30 秒跑一次。
"""

from __future__ import annotations

import datetime
import logging
import threading
import time

from app.config import get_settings
from app.strategy import realtime_hub as hub
from app.strategy.watch_alert import is_market_hours

logger = logging.getLogger(__name__)

_SCAN_INTERVAL = 30          # 秒
_VEL_PUSH_MOVE = 3.0         # 急拉推送阈值（5分钟涨速%）
_HEALTH_ALERT_AFTER = 180    # 盘中全推断流超此秒数 → Bark 告警
_health: dict = {"alerted": False, "outage_start": 0.0}    # 心跳状态
# 推送冷却（秒）：同一事件冷却内不重复；过冷却仍触发=再提醒；程度升级(事件key带档位)立即再推。
_COOLDOWN_DEFAULT = 1500     # 25分钟
_COOLDOWN = {
    "crash": 600, "limitbreak": 600,                         # 闪崩/炸板·风险复发快报(10min)
    "warn": 900, "taildown": 900, "hold": 900, "limitweak": 900,
    "surge": 1200, "vel": 1200, "tailup": 1200, "brk": 1200,  # 个股机会/突破破位(20min)
    "secin": 1500, "secout": 1500, "theme": 1500,            # 板块/题材(25min·另有跨档立即)
    "senti": 3600,                                           # 情绪状态转折(1小时·防flapping)
    "tailsummary": 999999,                                   # 尾盘小结·当天一次
}
_pushed_date = ""
_pushed: dict[str, float] = {}        # key -> 上次推送 epoch（冷却判断）
_sealed: dict = {}                    # 当前封涨停集合 {code:{peak,name}}·跨扫描持续·炸板检测用
_thread: threading.Thread | None = None
_stop = threading.Event()


def _cooldown_sec(key: str) -> int:
    """按事件类型取冷却秒数（key 前缀决定）。"""
    return _COOLDOWN.get(key.split("_", 1)[0], _COOLDOWN_DEFAULT)


def _should_push(key: str, now: float) -> bool:
    """未推过、或距上次推送已过冷却 → 可推。"""
    last = _pushed.get(key)
    return last is None or (now - last) >= _cooldown_sec(key)


def _dedup_reset_if_new_day() -> None:
    global _pushed_date, _pushed
    today = datetime.date.today().isoformat()
    if today != _pushed_date:
        _pushed_date, _pushed = today, {}
        _sealed.clear()               # 新交易日重置封板集合


def _stock_url(ts_code: str) -> str:
    base = get_settings().web_base_url.rstrip("/")
    return f"{base}/stock?code={ts_code[:6]}" if base else ""


def _collect_events() -> list[tuple[str, str, str, str]]:
    """汇总待推事件 [(dedup_key, title, body, ts_code)]。全市场视角：板块→龙头→资金。"""
    from app.strategy.realtime_fund import (detect_limit_breaks, detect_theme_fermentation,
                                            fund_surge_events, sector_board,
                                            sector_flow_events, velocity_events)
    from app.strategy.realtime_fund import (altitude_risk, detect_breakouts, rel_strength_tag,
                                            sentiment_thermometer, tech_context)
    df = hub.snapshot().to_df()
    imap = hub.industry_map()
    tech = hub.tech_map()                                                   # 技术姿态+关键位数值+昨收连板(因子表v16)
    rows = df.to_dict("records")
    sec_avg = _sector_avg_map(rows, imap)                                    # 板块均涨(个股相对强度用)
    events: list[tuple[str, str, str, str]] = []
    consec = {c: (t.get("consec_limit_now") or 0) for c, t in tech.items()}
    senti = sentiment_thermometer(rows, consec)
    events += _sentiment_events(senti)                                      # 情绪转折(退潮/冰点/高潮)
    mkt = _mkt_warn(senti.get("state", ""))                                 # 大盘环境警示(挂到机会信号·别逆势追)
    breaks, new_sealed = detect_limit_breaks(rows, _sealed)                  # 龙头炸板/开板预警
    _sealed.clear(); _sealed.update(new_sealed)
    events += breaks
    events += _breakout_events(detect_breakouts(rows, hub.past_prices(5.0), tech), tech, mkt)   # 实时突破/破位
    events += _flash_events(rows, tech)                                     # 个股急跌/闪崩(带技术位)
    events += _theme_events(detect_theme_fermentation(rows, hub.concept_map()))   # 题材发酵
    events += _tail_events(rows, imap)                                       # 尾盘异动(14:30后)
    events += _sector_events(sector_flow_events(sector_board(df, imap)))     # 板块资金涌入/撤离
    events += _surge_events(fund_surge_events(df), imap, tech, sec_avg, mkt)  # 个股资金抢筹(大盘+板块+技术+高位/相对强度)
    for v in velocity_events(hub.snapshot().prices(), hub.past_prices(5.0), min_move=_VEL_PUSH_MOVE):
        q = hub.snapshot().get(v["ts_code"]) or {}
        if float(q.get("vol_ratio") or 0) < 1.5:                            # 放量确认·过滤无量急拉(对倒/诱多)
            continue
        ind = imap.get(v["ts_code"], "")
        extra = "·".join(x for x in (_stock_context_tags(q, tech.get(v["ts_code"]), sec_avg.get(ind)), mkt) if x)
        body = f"5分钟拉升 +{v['move']}%·量比{q.get('vol_ratio', '')}·现价{q.get('price', '')}"
        events.append((f"vel_{v['ts_code']}", f"⚡ 急拉·{q.get('name', v['ts_code'])}{('·'+ind) if ind else ''}",
                       body + (f"·{extra}" if extra else ""), v["ts_code"]))
    events += _holding_events()
    return events


def _mkt_warn(state: str) -> str:
    """大盘环境警示（仅退潮/冰点时挂到机会信号·提醒别逆势追高）。"""
    return f"⚠大盘{state}" if state in ("退潮分歧", "冰点") else ""


def _sector_avg_map(rows: list[dict], imap: dict) -> dict:
    """{行业: 板块均涨幅}（成分≥3·个股相对强度用）。"""
    from collections import defaultdict
    agg: dict = defaultdict(lambda: [0.0, 0])
    for r in rows:
        ind = imap.get(r["ts_code"], "")
        if ind:
            try:
                agg[ind][0] += float(r.get("pct_chg") or 0)
                agg[ind][1] += 1
            except (TypeError, ValueError):
                pass
    return {k: v[0] / v[1] for k, v in agg.items() if v[1] >= 3}


def _stock_context_tags(q: dict, t: dict | None, sector_avg) -> str:
    """个股上下文：技术位 + 高位风险 + 相对板块强弱（拼成一串）。"""
    from app.strategy.realtime_fund import altitude_risk, rel_strength_tag, tech_context
    price, prev = q.get("price"), q.get("prev_close")
    parts = [tech_context(price, prev, t), altitude_risk(price or 0, prev or 0, t),
             rel_strength_tag(float(q.get("pct_chg") or 0), sector_avg)]
    return "·".join(p for p in parts if p)


def _sentiment_events(s: dict) -> list[tuple[str, str, str, str]]:
    """情绪转折推送：仅在 退潮分歧/冰点(风险) 或 高潮过热 时推（决定能否打板追高）。"""
    if s.get("state") not in ("退潮分歧", "冰点", "高潮过热"):
        return []
    body = (f"{s['emoji']}情绪{s['state']}·空间板{s['top_board']}板"
            f"{('·'+s['top_name']) if s['top_name'] else ''}·晋级率{s['promo_rate']}%"
            f"·炸板率{s['bao_rate']}%·赚钱效应{s['promo_premium']:+.1f}%")
    return [(f"senti_{s['state']}", f"🌡️ 情绪·{s['state']}", body, s.get("top_code", ""))]


def _breakout_events(breaks: list[dict], tech: dict, mkt: str = "") -> list[tuple[str, str, str, str]]:
    """实时突破(机会)/破位(风险)关键位推送；突破附高位风险 + 大盘环境(高位突破/逆势=追高)。"""
    from app.strategy.realtime_fund import altitude_risk
    out: list[tuple[str, str, str, str]] = []
    for b in breaks:
        if b["dir"] == "up":
            q = hub.snapshot().get(b["ts_code"]) or {}
            alt = altitude_risk(q.get("price") or 0, q.get("prev_close") or 0, tech.get(b["ts_code"]))
            warn = "·".join(x for x in (("⚠" + alt) if alt else "", mkt) if x)
            out.append((f"brk_up_{b['ts_code']}", f"📈 突破·{b['name']}",
                        f"{b['what']}·现价{b['price']}·涨{b['pct_chg']:+.1f}%"
                        f"{('·'+warn) if warn else ''}（关键位·昨收口径）", b["ts_code"]))
        else:
            out.append((f"brk_dn_{b['ts_code']}", f"📉 破位·{b['name']}",
                        f"{b['what']}·现价{b['price']}·{b['pct_chg']:+.1f}%·留意（关键位）", b["ts_code"]))
    return out


def _mag_tier(net: float) -> int:
    """资金量级档位(亿)：key 含档位 → 跨档=升级，立即再推(不等冷却)。"""
    for t in (40, 25, 15, 8, 3):
        if abs(net) >= t:
            return t
    return 3


def _sector_events(flow: list[dict]) -> list[tuple[str, str, str, str]]:
    """板块资金事件：涌入(机会)/撤离(风险)，均点名龙头；key 含量级档位支持升级再推。"""
    out: list[tuple[str, str, str, str]] = []
    for s in flow:
        tier = _mag_tier(s["net_yi"])
        if s["kind"] == "in":
            out.append((f"secin_{s['industry']}_{tier}", f"🔥 资金涌入·{s['industry']}",
                        f"板块主动净买 +{s['net_yi']}亿·均涨{s['avg_pct']}%·龙头 {s['leader']} "
                        f"{s['leader_pct']:+.1f}%（L1估算）", s["leader_code"]))
        else:
            out.append((f"secout_{s['industry']}_{tier}", f"⚠️ 资金撤离·{s['industry']}",
                        f"板块主动净卖 {s['net_yi']}亿·均跌{s['avg_pct']}%·龙头 {s['leader']} "
                        f"{s['leader_pct']:+.1f}%·留意退潮", s["leader_code"]))
    return out


def _surge_events(surge: list[dict], imap: dict, tech: dict, sec_avg: dict,
                  mkt: str = "") -> list[tuple[str, str, str, str]]:
    """个股资金抢筹（大盘环境 + 板块 + 技术位/MACD + 资金持续 + 高位/相对强度·多维交叉判真龙头 vs 跟风追高）。"""
    from app.strategy.realtime_fund import fund_flow_quality
    out: list[tuple[str, str, str, str]] = []
    for s in surge:
        ind = imap.get(s["ts_code"], "")
        q = hub.snapshot().get(s["ts_code"]) or {}
        fq = fund_flow_quality(hub.net_series(s["ts_code"]))     # 资金持续/脉冲退潮
        tg = _stock_context_tags(q, tech.get(s["ts_code"]), sec_avg.get(ind))
        tags = "·".join(x for x in (fq, tg, mkt) if x)
        out.append((f"surge_{s['ts_code']}", f"💰 资金抢筹·{s['name']}{('·'+ind) if ind else ''}",
                    f"外盘{s['outer_ratio']*100:.0f}%·量比{s['vol_ratio']}·涨{s['pct_chg']}%"
                    f"·主动净买{s['net_yi']}亿{('·'+tags) if tags else ''}（L1估算·非龙虎榜真钱）", s["ts_code"]))
    return out


def _holding_codes() -> set:
    """当前持仓代码集合（持仓闪崩高优先级用）。"""
    from app.strategy import db
    return {w["ts_code"] for w in db.get_watchlist() if w.get("is_holding")}


def _flash_events(rows: list[dict], tech: dict) -> list[tuple[str, str, str, str]]:
    """个股急跌/闪崩预警(带技术位:跌破均线更危险 vs 回踩支撑)。持仓命中→最高优先级。"""
    from app.strategy.realtime_fund import detect_flash_crashes, tech_context
    held = _holding_codes()
    out: list[tuple[str, str, str, str]] = []
    for f in detect_flash_crashes(rows, hub.past_prices(3.0)):
        h = f["ts_code"] in held
        q = hub.snapshot().get(f["ts_code"]) or {}
        tg = tech_context(q.get("price"), q.get("prev_close"), tech.get(f["ts_code"]))
        if f["tier"] == "crash":
            title = f"{'🚨 持仓闪崩·' if h else '💥 闪崩·'}{f['name']}"
            body = (f"3分钟急跌 {f['drop']}%·放量主动砸(内盘{(1 - f['outer_ratio']) * 100:.0f}%·"
                    f"量比{f['vol_ratio']})·全天{f['pct_chg']:+.1f}%{('·'+tg) if tg else ''}")
            if h:
                body += "\n→ 你的持仓，立刻走一遍「拿得住」冷静判断"
            out.append((f"crash_{f['ts_code']}", title, body, f["ts_code"]))
        else:
            out.append((f"warn_{f['ts_code']}", f"{'⚠️ 持仓急跌·' if h else '⚡ 急跌·'}{f['name']}",
                        f"3分钟急跌 {f['drop']}%{('·'+tg) if tg else ''}·留意是否放量主动砸", f["ts_code"]))
    return out


def _tail_events(rows: list[dict], imap: dict) -> list[tuple[str, str, str, str]]:
    """尾盘异动(相对14:30)：拉升(抢明天)/跳水(出货)，14:55后附尾盘小结。"""
    if not hub.is_tail_session():
        return []
    hub.record_tail_baseline(rows)                       # 进入尾盘首次记基准(幂等)
    base = hub.tail_baseline()
    if not base:
        return []
    from app.strategy.realtime_fund import tail_movers
    out: list[tuple[str, str, str, str]] = []
    for m in tail_movers(rows, base):
        if m["kind"] == "up":
            out.append((f"tailup_{m['ts_code']}", f"🚀 尾盘拉升·{m['name']}",
                        f"尾盘 +{m['move']}%·尾盘主动净买 +{m['net_tail']}亿·全天{m['pct_chg']:+.1f}%·或抢明天", m["ts_code"]))
        else:
            out.append((f"taildown_{m['ts_code']}", f"📉 尾盘跳水·{m['name']}",
                        f"尾盘 {m['move']}%·尾盘主动净卖 {m['net_tail']}亿·留意主力出货", m["ts_code"]))
    out += _tail_summary(rows, imap, base)
    return out


def _tail_summary(rows: list[dict], imap: dict, base: dict) -> list[tuple[str, str, str, str]]:
    """尾盘小结(14:55后一次)：资金流入板块TOP + 尾盘拉升/跳水个股 → 定明天。"""
    if time.strftime("%H%M") < "1455":
        return []
    from app.strategy.realtime_fund import tail_movers, tail_sector_flow
    sec = tail_sector_flow(rows, base, imap, top=3)
    mv = tail_movers(rows, base)
    ups = [m for m in mv if m["kind"] == "up"][:3]
    downs = [m for m in mv if m["kind"] == "down"][:3]
    lines = []
    if sec:
        lines.append("资金流入板块: " + "、".join(f"{s['industry']}+{s['net_tail']}亿" for s in sec))
    if ups:
        lines.append("尾盘拉升: " + "、".join(f"{m['name']}+{m['move']}%" for m in ups))
    if downs:
        lines.append("尾盘跳水: " + "、".join(f"{m['name']}{m['move']}%" for m in downs))
    if not lines:
        return []
    return [("tailsummary", "🕒 尾盘小结·定明天", "\n".join(lines) + "\n（14:30→收盘·仅供观察）", "")]


def _theme_events(themes: list[dict]) -> list[tuple[str, str, str, str]]:
    """题材发酵推送（按异动家数分档去重：扩散到更高档可再推一次）。"""
    out: list[tuple[str, str, str, str]] = []
    for t in themes:
        level = 8 if t["n_hot"] >= 8 else (5 if t["n_hot"] >= 5 else 3)
        leads = "/".join(f"{l['name']}{l['pct']:+.0f}%" for l in t["leaders"])
        out.append((f"theme_{t['theme']}_{level}", f"🔥 题材发酵·{t['theme']}",
                    f"{t['n_hot']}只异动·均涨{t['avg_pct']}%·领涨 {leads}", t.get("lead_code", "")))
    return out


def _holding_events() -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    for h in hub.build_board().get("holdings", []):
        if h["label"] in ("留意", "风险"):
            out.append((f"hold_{h['ts_code']}_{h['label']}", f"🚨 持仓·{h['name']} {h['label']}",
                        f"{h['reason']}·现{h['pct_chg']}%·外盘{h['outer_ratio']*100:.0f}%"
                        f"\n→ 别冲动，先走一遍「拿得住」冷静流程", h["ts_code"]))
    return out


def scan_once(force: bool = False, push: bool = True) -> list[dict]:
    """扫一次 → 推【过冷却 / 升级到新档】的事件。返回新推列表。"""
    if not force and (not is_market_hours() or not hub.is_live()):
        return []
    _dedup_reset_if_new_day()
    from app.notify.notifier import push_bark
    now = time.time()
    new: list[dict] = []
    for key, title, body, code in _collect_events():
        if not _should_push(key, now):
            continue
        if (not push) or push_bark(title, body, group="实时盯盘",
                                   url=_stock_url(code), level=_bark_level(key, title)):
            _pushed[key] = now
            new.append({"key": key, "title": title, "body": body})
    return new


def _bark_level(key: str, title: str) -> str:
    """信号重要度 → Bark 级别：timeSensitive(穿透勿扰) / active(正常) / passive(静默)。"""
    if "持仓" in title:                                     # 你的持仓·最该打断
        return "timeSensitive"
    p = key.split("_", 1)[0]
    if p in ("crash", "limitbreak", "senti"):              # 闪崩/炸板/情绪转折=高优风险
        return "timeSensitive"
    if p == "theme" or key.startswith("brk_up"):           # 题材/个股突破=低优·静默
        return "passive"
    return "active"


def _health_decision(market_hours: bool, fullpush_live: bool, outage_sec: float,
                     alerted: bool) -> str:
    """心跳决策（纯函数·可测）：'alert'(首次断流告警) / 'recover'(恢复通知) / 'reset' / 'hold'。"""
    if not market_hours:
        return "reset"
    if fullpush_live:
        return "recover" if alerted else "reset"
    if outage_sec >= _HEALTH_ALERT_AFTER and not alerted:
        return "alert"
    return "hold"


def _push_health(title: str, body: str) -> None:
    from app.notify.notifier import push_bark
    base = get_settings().web_base_url.rstrip("/")
    push_bark(title, body, group="盯盘", level="timeSensitive",   # 断流/恢复=穿透勿扰
              url=(f"{base}/realtime" if base else ""))


def _health_check() -> None:
    """心跳：盘中全推断流 → 新浪兜底填快照（保命）+ 超时 Bark 告警 + 恢复通知。"""
    now = time.time()
    mkt = is_market_hours()
    live = hub.is_live() if mkt else True
    if mkt and not live:
        if not _health["outage_start"]:
            _health["outage_start"] = now
        hub.fallback_fill_from_sina()                       # 全推断了·新浪填快照·看板不空
    outage = (now - _health["outage_start"]) if _health["outage_start"] else 0.0
    d = _health_decision(mkt, live, outage, _health["alerted"])
    if d == "alert":
        _push_health("⚠️ 盯盘数据断流", f"全推已断约 {outage / 60:.0f} 分钟·已切新浪兜底"
                     "(仅涨跌幅·无内外盘资金)·看板仍可看")
        _health["alerted"] = True
    elif d in ("recover", "reset"):
        if d == "recover":
            _push_health("✅ 盯盘数据恢复", "全推已恢复实时供数·内外盘/资金维度恢复")
        _health.update(alerted=False, outage_start=0.0)


def _loop() -> None:
    while not _stop.is_set():
        try:
            _health_check()                                 # 心跳:断流告警+新浪兜底
            hub.record_history()
            hub.record_net_history()                        # 资金持续/脉冲采样
            scan_once()
        except Exception as e:
            logger.warning("[实时扫描] 异常：%s", e)
        _stop.wait(_SCAN_INTERVAL)


def start_scanner() -> None:
    """启动后台扫描线程（幂等）。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="realtime-scan", daemon=True)
    _thread.start()


def stop_scanner() -> None:
    _stop.set()
