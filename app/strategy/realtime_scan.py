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
    "shift": 900,                                            # 重大变化:板块资金异常加速/大盘转向(15min·防刷屏)
    "dip": 1800,                                             # 自选低吸/企稳观察(30min·同一票别反复提)
    "reg": 7200,                                             # 停牌/异动核查(2h·变化慢·别刷屏)
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
    """汇总待推事件 [(dedup_key, title, body, ts_code)]。全市场视角：板块→龙头→资金。

    **健壮性铁律**：每个子采集器独立 try/except 隔离——单个子项抛异常只记 traceback 并跳过，
    绝不能拖垮整轮扫描(否则一个 bug = 全盘静默无推送·2026-06-30 盘中踩坑)。
    """
    from app.strategy.realtime_fund import (detect_limit_breaks, detect_theme_fermentation,
                                            fund_surge_events, sector_board,
                                            sector_flow_events, velocity_events)
    from app.strategy.realtime_fund import (altitude_risk, detect_breakouts, rel_strength_tag,
                                            sentiment_thermometer, tech_context)
    df = hub.stock_df()                                  # 只 A股个股(剔指数/转债/ETF)
    imap = hub.industry_map()
    tech = hub.tech_map()                                                   # 技术姿态+关键位数值+昨收连板(因子表v16)
    rows = df.to_dict("records")
    sec_avg = _sector_avg_map(rows, imap)                                    # 板块均涨(个股相对强度用)
    events: list[tuple[str, str, str, str]] = []

    def _add(label: str, producer) -> None:
        """跑一个子采集器，结果并入 events；失败只记完整 traceback 不中断其余。"""
        try:
            ev = producer()
            if ev:
                events.extend(ev)
        except Exception:
            logger.exception("[实时扫描] 子项「%s」失败(已隔离·跳过)", label)

    consec = {c: (t.get("consec_limit_now") or 0) for c, t in tech.items()}
    senti = sentiment_thermometer(rows, consec)
    mkt = _mkt_warn(senti.get("state", ""))                                 # 大盘环境警示(挂到机会信号·别逆势追)

    def _breaks():                                                          # 龙头炸板/开板·需回写 _sealed
        breaks, new_sealed = detect_limit_breaks(rows, _sealed)
        _sealed.clear(); _sealed.update(new_sealed)
        return breaks

    board = sector_board(df, imap)
    _add("情绪转折", lambda: _sentiment_events(senti))                       # 退潮/冰点/高潮
    _add("炸板/开板", _breaks)
    _add("突破/破位", lambda: _breakout_events(detect_breakouts(rows, hub.past_prices(5.0), tech), tech, mkt, imap, sec_avg))
    _add("急跌/闪崩", lambda: _flash_events(rows, tech, imap, sec_avg))
    _add("题材发酵", lambda: _theme_events(detect_theme_fermentation(rows, hub.concept_map())))
    _add("尾盘异动", lambda: _tail_events(rows, imap))                       # 14:30后
    _add("板块资金", lambda: _sector_events(sector_flow_events(board)))      # 涌入/撤离
    _add("重大变化", lambda: _market_shift_events(board, df, imap))          # 板块资金异常加速/大盘转向
    _add("资金抢筹", lambda: _surge_events(fund_surge_events(df), imap, tech, sec_avg, mkt))
    _add("急拉", lambda: _velocity_block(imap, sec_avg, mkt))
    _add("持仓异动", _holding_events)
    _add("低吸观察", _watch_dip_events)                                      # 自选/持仓回调企稳
    _add("停牌/监管", _reg_events)                                          # 自选/持仓 停牌/连板异动核查
    return events


def _velocity_block(imap: dict, sec_avg: dict, mkt: str) -> list[tuple[str, str, str, str]]:
    """个股急拉：5分钟涨速达阈值 + 放量确认(量比≥1.5·过滤对倒/诱多)→ 现价+板块+大盘环境。"""
    from app.strategy.realtime_fund import velocity_events
    out: list[tuple[str, str, str, str]] = []
    for v in velocity_events(hub.snapshot().prices(), hub.past_prices(5.0), min_move=_VEL_PUSH_MOVE):
        q = hub.snapshot().get(v["ts_code"]) or {}
        if float(q.get("vol_ratio") or 0) < 1.5:
            continue
        ind = imap.get(v["ts_code"], "")
        stock = f"现价{q.get('price', '')}·5分钟+{v['move']}%·量比{q.get('vol_ratio', '')}"
        nm = q.get("name", v["ts_code"])
        out.append((f"vel_{v['ts_code']}", f"⚡ 急拉·{nm}",
                    _stock_lines(nm, stock, _sec_line(ind, sec_avg.get(ind)), mkt), v["ts_code"]))
    return out


def _reg_events() -> list[tuple[str, str, str, str]]:
    """自选/持仓 停牌(事实) 或 高位连板异动核查风险(派生·含今日封板) → 即时提醒。"""
    from app.data.composite_provider import CompositeProvider
    from app.strategy.reg_risk import reg_flag
    out: list[tuple[str, str, str, str]] = []
    tech = hub.tech_map()
    provider = CompositeProvider()
    for code, meta in hub.watch_meta().items():
        consec = int((tech.get(code) or {}).get("consec_limit_now") or 0)
        q = hub.snapshot().get(code)
        if q and float(q.get("pct_chg") or 0) >= 9.8:       # 今日又封板→实时连板+1
            consec += 1
        rf = reg_flag(code, meta.get("name", ""), consec, provider)
        if not rf:
            continue
        tag = "持仓" if meta.get("is_holding") else "自选"
        icon = "🔒" if rf["kind"] == "suspend" else "⚠️"
        out.append((f"reg_{rf['kind']}_{code}", f"{icon} {tag}监管·{meta.get('name', '')}",
                    f"{meta.get('name', '')} {rf['text']}", code))
    return out


def _watch_dip_events() -> list[tuple[str, str, str, str]]:
    """自选/持仓回调到支撑(MA20/20日低)且下跌动能衰竭(近5分钟企稳)→低吸观察。连续交易时段。"""
    from app.strategy.realtime_fund import watch_dip_signal
    out: list[tuple[str, str, str, str]] = []
    tech = hub.tech_map()
    past = hub.past_prices(5.0)
    for code, meta in hub.watch_meta().items():
        sig = watch_dip_signal(hub.snapshot().get(code), tech.get(code), past.get(code),
                               name=meta.get("name", ""))
        if not sig:
            continue
        tag = "持仓" if meta.get("is_holding") else "自选"
        body = (f"{sig['name']} 现{sig['price']}·{sig['pos']}(乖离{sig['bias20']:+}%)·"
                f"跌势企稳(5分钟{sig['recent']:+}%)·外盘{sig['outer']}%承接 — 回调到位·自行判断")
        out.append((f"dip_{code}", f"🟢 {tag}低吸观察·{sig['name']}", body, code))
    return out


def _market_shift_events(board: list, df, imap: dict) -> list[tuple[str, str, str, str]]:
    """重大变化即时事件：板块资金异常加速 / 大盘转向（较近窗口/30min前）。"""
    import pandas as pd

    from app.strategy.realtime_fund import breadth_trend, detect_market_shifts, sector_flow_delta
    secs_delta = sector_flow_delta(board, hub.sector_net_ago(5.0))
    pct = pd.to_numeric(df["pct_chg"], errors="coerce")
    b_now = {"up": int((pct > 0).sum()), "limit_up": int((pct >= 9.5).sum())}
    b_trend = breadth_trend(b_now, hub.breadth_ago(30.0))
    return detect_market_shifts(secs_delta, b_trend)


_PULSE_INTERVAL = 20 * 60        # 盘中市场快照·每~20分钟一条
_pulse_last = [0.0]              # 上次定时快照推送 epoch


def _maybe_push_pulse() -> None:
    """盘中定时市场快照：连续交易时段每~20分钟 Bark 一条（大盘趋势+板块资金加速/退潮+龙头）。"""
    if hub.market_session() != "continuous" or not hub.is_live():
        return
    now = time.time()
    if now - _pulse_last[0] < _PULSE_INTERVAL:
        return
    pulse = (hub.build_board_cached() or {}).get("pulse")     # 复用看板已算好的快照句
    if not pulse:
        return
    from app.notify.notifier import all_device_keys, push_bark
    base = get_settings().web_base_url.rstrip("/")
    if push_bark("📊 盘中市场快照", pulse, group="盘中摘要", level="active",
                 key=all_device_keys(), url=(f"{base}/realtime" if base else "")):   # 全市场→两台都收
        _pulse_last[0] = now


def _mkt_warn(state: str) -> str:
    """大盘行内容（仅退潮/冰点时挂到机会信号·别逆势）；行标签'大盘'由 _stock_lines 加。"""
    return {"退潮分歧": "退潮分歧·慎追", "冰点": "冰点·空仓观望"}.get(state, "")


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
    body = (f"空间板{s['top_board']}板{('·'+s['top_name']) if s['top_name'] else ''}"
            f"·赚钱效应{s['promo_premium']:+.1f}%·炸板率{s['bao_rate']}%")
    return [(f"senti_{s['state']}", f"🌡️ 情绪·{s['state']}", body, s.get("top_code", ""))]


def _breakout_events(breaks: list[dict], tech: dict, mkt: str = "", imap: dict | None = None,
                     sec_avg: dict | None = None) -> list[tuple[str, str, str, str]]:
    """实时突破(机会)/破位(风险)关键位推送；个股/板块/大盘分块。"""
    from app.strategy.realtime_fund import altitude_risk
    imap = imap or {}
    sec_avg = sec_avg or {}
    out: list[tuple[str, str, str, str]] = []
    for b in breaks:
        ind = imap.get(b["ts_code"], "")
        sec = _sec_line(ind, sec_avg.get(ind))
        if b["dir"] == "up":
            q = hub.snapshot().get(b["ts_code"]) or {}
            alt = altitude_risk(q.get("price") or 0, q.get("prev_close") or 0, tech.get(b["ts_code"]))
            stock = f"{b['what']}·现价{b['price']}·涨{b['pct_chg']:+.1f}%" + (f"·⚠{alt.split('·')[0]}" if alt else "")
            out.append((f"brk_up_{b['ts_code']}", f"📈 突破·{b['name']}",
                        _stock_lines(b["name"], stock, sec, mkt), b["ts_code"]))
        else:
            stock = f"{b['what']}·现价{b['price']}·{b['pct_chg']:+.1f}%"
            out.append((f"brk_dn_{b['ts_code']}", f"📉 破位·{b['name']}",
                        _stock_lines(b["name"], stock, sec), b["ts_code"]))
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
    """个股资金抢筹：现价+涨幅+主动净买 ｜ 板块情况(均涨+领涨/弱于)+风险。详细全维看看板。"""
    from app.strategy.realtime_fund import altitude_risk, fund_flow_quality, rel_strength_tag
    out: list[tuple[str, str, str, str]] = []
    for s in surge:
        ind = imap.get(s["ts_code"], "")
        q = hub.snapshot().get(s["ts_code"]) or {}
        t = tech.get(s["ts_code"]) or {}
        # 个股行：量价本体 + 强度 + 个股风险
        stock = (f"现价{q.get('price', '')}·涨{s['pct_chg']}%·净买{s['net_yi']}亿"
                 f"·外盘{s['outer_ratio'] * 100:.0f}%·量比{s['vol_ratio']}")
        rps = t.get("rps120")
        if rps is not None and rps == rps:
            stock += f"·RPS{int(float(rps))}"
        if fund_flow_quality(hub.net_series(s["ts_code"])) == "脉冲退潮":
            stock += "·⚠脉冲退潮"
        alt = altitude_risk(q.get("price") or 0, q.get("prev_close") or 0, t)
        if alt:
            stock += "·⚠" + alt.split("·")[0]
        sec = _sec_line(ind, sec_avg.get(ind), rel_strength_tag(s["pct_chg"], sec_avg.get(ind)))
        out.append((f"surge_{s['ts_code']}", f"💰 资金抢筹·{s['name']}",
                    _stock_lines(s["name"], stock, sec, mkt), s["ts_code"]))
    return out


def _stock_lines(name: str, stock: str, sec: str = "", mkt: str = "") -> str:
    """个股(用股票名当标签) / 板块 / 大盘 分块（▸ 分隔·一行内·紧凑又一眼可分）。"""
    blocks = [f"{name} {stock}"]
    if sec:
        blocks.append(f"板块 {sec}")
    if mkt:
        blocks.append(f"大盘 {mkt}")
    return " ▸ ".join(blocks)


def _sec_line(ind: str, sec_avg, rel: str = "") -> str:
    """板块行内容：板块名·均涨·领涨/弱于（行标签'板块'由 _stock_lines 加）。"""
    if not ind:
        return ""
    parts = [ind]
    if sec_avg is not None:
        parts.append(f"均涨{sec_avg:+.1f}%")
    if rel == "领涨板块":
        parts.append("领涨")
    elif rel == "弱于板块":
        parts.append("⚠弱于")
    return "·".join(parts)


def _holding_codes() -> set:
    """当前持仓代码集合（持仓闪崩高优先级用）。"""
    from app.strategy import db
    return {w["ts_code"] for w in db.get_watchlist() if w.get("is_holding")}


def _flash_events(rows: list[dict], tech: dict, imap: dict,
                  sec_avg: dict) -> list[tuple[str, str, str, str]]:
    """个股急跌/闪崩预警：现价+跌速+主动砸 + 板块情况。持仓命中→最高优先级 + 走拿得住。"""
    from app.strategy.realtime_fund import detect_flash_crashes
    held = _holding_codes()
    out: list[tuple[str, str, str, str]] = []
    for f in detect_flash_crashes(rows, hub.past_prices(3.0)):
        h = f["ts_code"] in held
        ind = imap.get(f["ts_code"], "")
        q = hub.snapshot().get(f["ts_code"]) or {}
        sec = _sec_line(ind, sec_avg.get(ind))
        if f["tier"] == "crash":
            title = f"{'🚨 持仓闪崩·' if h else '💥 闪崩·'}{f['name']}"
            stock = f"现价{q.get('price', '')}·3分钟急跌 {f['drop']}%·放量主动砸"
            out.append((f"crash_{f['ts_code']}", title, _stock_lines(f["name"], stock, sec), f["ts_code"]))
        else:
            out.append((f"warn_{f['ts_code']}", f"{'⚠️ 持仓急跌·' if h else '⚡ 急跌·'}{f['name']}",
                        _stock_lines(f["name"], f"现价{q.get('price', '')}·3分钟急跌 {f['drop']}%", sec), f["ts_code"]))
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
                        f"尾盘 +{m['move']}%·尾盘主动净买 +{m['net_tail']}亿·全天{m['pct_chg']:+.1f}%", m["ts_code"]))
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
                        f"{h['reason']}·现{h['pct_chg']}%·外盘{h['outer_ratio']*100:.0f}%", h["ts_code"]))
    return out


_AUC_SECTOR_TH = 2.0          # 板块竞价均高开 ≥ 此值(%)才推"竞价强势板块"


def _auction_events() -> list[tuple[str, str, str, str]]:
    """集合竞价时段(9:15-9:30) 全市场信号(纯价格口径)：自选/持仓异动 + 板块竞价强弱 + 竞价情绪。

    竞价无连续成交，内外盘/量比/急拉/闪崩等量资金信号不可用→留连续档；价格类全市场信号照跑。
    """
    from app.strategy.realtime_fund import (auction_alerts, auction_sector_strength,
                                            auction_sentiment)
    rows = hub.stock_df().to_dict("records")             # 只 A股个股(剔指数/转债/ETF)
    imap = hub.industry_map()
    events = list(auction_alerts(rows, hub.watch_meta()))                 # ① 自选/持仓优先(含委比承接)
    for s in auction_sector_strength(rows, imap, top=5):                  # ② 板块方向(均高开+热度额+委比)
        if s["avg_gap"] >= _AUC_SECTOR_TH:
            flow = f"·委比{'+' if s['entrust'] >= 0 else ''}{s['entrust']}%({'承接' if s['entrust'] > 0 else '抛压'})" if s["entrust"] else ""
            events.append((f"auc_sec_{s['industry']}", f"🔆 竞价强势板块·{s['industry']}",
                           f"{s['industry']} 均高开+{s['avg_gap']}%·额{s['amount_yi']}亿{flow}·"
                           f"龙头 {s['leader']} +{s['leader_pct']}%", s["leader_code"]))
    se = auction_sentiment(rows)                                          # ③ 全市场竞价情绪
    if se:
        events.append(("auc_senti", "🌅 集合竞价情绪",
                       f"高开{se['up']}/低开{se['down']} · 竞价涨停{se['limit_up']}只 · {se['state']}", ""))
    return events


def scan_once(force: bool = False, push: bool = True) -> list[dict]:
    """扫一次 → 推【过冷却 / 升级到新档】的事件。返回新推列表。

    时段感知：集合竞价(9:15-9:30)只推自选/持仓竞价异动；连续竞价跑全市场信号。
    """
    sess = "continuous" if force else hub.market_session()
    if not force and (sess == "closed" or not hub.is_live()):
        return []
    _dedup_reset_if_new_day()
    from app.notify.notifier import push_bark
    now = time.time()
    new: list[dict] = []
    events = _auction_events() if sess in hub._AUCTION_SESSIONS else _collect_events()
    _diag_log(len(events), sess)                     # 周期诊断(~5min)·排查"推送很少"
    for key, title, body, code in events:
        if not _should_push(key, now):
            continue
        bark_key = _route_keys(key, code)
        if bark_key is None:                         # 个性化信号·归属人没配设备 → 跳过(守"各推各的")
            continue
        if (not push) or push_bark(title, body, group="实时盯盘", key=bark_key,
                                   url=_stock_url(code), level=_bark_level(key, title)):
            _pushed[key] = now
            new.append({"key": key, "title": title, "body": body})
    return new


_scan_diag = [0.0]


def _diag_log(n_events: int, sess: str) -> None:
    """每~5分钟记一条盯盘诊断：时段/是否live/快照A股数/本轮事件数——排查"推送很少"卡在取数还是产事件。"""
    now = time.time()
    if now - _scan_diag[0] < 300:
        return
    _scan_diag[0] = now
    try:
        n_snap = len(hub.stock_df())
    except Exception:
        n_snap = -1
    logger.info("[盯盘诊断] 时段=%s live=%s 快照A股=%d只 本轮事件=%d", sess, hub.is_live(), n_snap, n_events)


# 个性化信号前缀(关于某只自选/持仓票)：按归属人路由·只推关注它的人；其余=全市场信号·全设备全量
_PERSONAL_PREFIXES = ("reg", "dip", "hold", "auc")


def _route_keys(key: str, code: str) -> str | None:
    """信号 → 目标 Bark key。全市场信号返回 ''(=push_bark 默认全设备)；个性化信号返回该票归属人的设备 key；
    个性化但归属人没配设备 → 返回 None(调用方跳过·不回落全量·守"各推各的")。"""
    from app.notify.notifier import all_device_keys, owner_device_keys
    if key.split("_", 1)[0] not in _PERSONAL_PREFIXES:
        return all_device_keys() or ""               # 全市场 → 两台都收
    owners = (hub.watch_meta().get(code) or {}).get("owners") or ()
    keys = owner_device_keys(owners)
    return keys or None                              # 归属人无设备 → None(跳过)


def _bark_level(key: str, title: str) -> str:
    """信号重要度 → Bark 级别：timeSensitive(穿透勿扰) / active(正常) / passive(静默)。"""
    if "持仓" in title:                                     # 你的持仓·最该打断
        return "timeSensitive"
    p = key.split("_", 1)[0]
    if p in ("crash", "limitbreak", "senti", "reg"):       # 闪崩/炸板/情绪转折/停牌异动=高优风险
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
            hub.record_market_history()                      # 板块资金/大盘广度趋势采样(变化/轮动用)
            scan_once()
            _maybe_push_pulse()                              # 定时市场快照(每~20min)
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
