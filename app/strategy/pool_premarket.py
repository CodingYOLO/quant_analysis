"""盘前·选股池消息面体检。

设计动机（与用户确认）：
- 选股池吃的是交易日量价/资金数据；周末(周五收→周一开)没有新交易数据，
  重跑技术选股结果不变——所以"更新"的不该是技术池，而是**消息面**。
- 本模块在每个交易日开盘前，对"最近交易日的选股池"逐只做消息面体检：
  把隔夜/周末新出的结构化公告(减持/解禁/业绩预告/快报/大宗/回购)归类为
  利好↑/利空↓，并附博查舆情(带来源)，输出报告 + 盘前 Bark 提醒。周一一跑自然覆盖整个周末。

数据口径（诚实·铁律）：
- 利好/利空判定**只来自结构化公告**(Tushare·确定性·可核查)，不靠 LLM 臆测涨跌。
- 博查舆情仅作"近期消息"上下文呈现(带媒体+日期来源)，不自动打多空标签。
- 不预测涨跌、不构成买卖建议；最终由人决策。
"""

from __future__ import annotations

import datetime
import logging

from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.strategy import db

logger = logging.getLogger(__name__)

# 解禁视为抛压提醒的时间窗（天）；比例单位随 Tushare，不做阈值判断，只用临近度门控
_FLOAT_SOON_DAYS = 30
# 大宗折溢价绝对值阈值（%）：超过才计入信号，过滤噪音
_BLOCK_PREMIUM_TH = 3.0
# 博查舆情逐只检索的上限（控成本；池子通常≤20只，足够覆盖）
_ALERT_CAP = 20


def _classify_events(events: dict | None, forecast: dict | None) -> dict:
    """从结构化事件 + 业绩预告 推导利好/利空信号（确定性·全公告口径·纯函数·可测）。

    Args:
        events: fundamentals._events_summary 结果（float/holder_trade/express/block/repurchase…）
        forecast: fundamentals._latest_forecast 结果（type/level/net_change）

    Returns:
        {"verdict": 利空|利好|混合|中性, "ups": [{text,src}], "downs": [{text,src}]}
    """
    ups: list[dict] = []
    downs: list[dict] = []
    ev = events or {}
    fc = forecast or {}

    # 业绩预告（前瞻·强信号）
    if fc.get("type"):
        tag = "业绩预告 " + fc["type"] + (f" {fc['net_change']}" if fc.get("net_change") else "")
        if fc.get("level") == "good":
            ups.append({"text": tag, "src": "Tushare业绩预告"})
        elif fc.get("level") == "bad":
            downs.append({"text": tag, "src": "Tushare业绩预告"})

    # 业绩快报（已出数·强信号）
    ex = ev.get("express") or {}
    if ex.get("net_profit_yoy") is not None:
        yoy = ex["net_profit_yoy"]
        t = f"快报净利同比{'+' if yoy >= 0 else ''}{yoy}%"
        (ups if yoy >= 0 else downs).append({"text": t, "src": "Tushare业绩快报"})

    # 增减持
    ht = ev.get("holder_trade") or {}
    de, inn = int(ht.get("de_count") or 0), int(ht.get("in_count") or 0)
    if de > inn:
        downs.append({"text": f"股东减持{de}次", "src": "Tushare增减持"})
    elif inn > de:
        ups.append({"text": f"股东增持{inn}次", "src": "Tushare增减持"})

    # 解禁（仅按临近度门控为抛压提醒，不臆测比例单位）
    fl = ev.get("float") or {}
    nd = fl.get("next_days")
    if nd is not None and 0 <= nd <= _FLOAT_SOON_DAYS:
        downs.append({"text": f"{nd}天后解禁(比例{fl.get('next_ratio')})", "src": "Tushare限售解禁"})

    # 大宗折溢价
    bl = ev.get("block") or {}
    p = bl.get("premium_avg")
    if p is not None and abs(p) >= _BLOCK_PREMIUM_TH:
        if p < 0:
            downs.append({"text": f"大宗折价{p}%(抛压)", "src": "Tushare大宗交易"})
        else:
            ups.append({"text": f"大宗溢价+{p}%(接盘)", "src": "Tushare大宗交易"})

    # 回购（仅实施中/完成才算实质利好）
    rp = ev.get("repurchase") or {}
    if rp.get("is_real"):
        amt = f"·{rp['amount_yi']}亿" if rp.get("amount_yi") else ""
        ups.append({"text": f"回购{rp.get('proc')}{amt}", "src": "Tushare回购"})

    if ups and downs:
        verdict = "混合"
    elif downs:
        verdict = "利空"
    elif ups:
        verdict = "利好"
    else:
        verdict = "中性"
    return {"verdict": verdict, "ups": ups, "downs": downs}


def _latest_pool_date() -> str | None:
    """最近一个已生成选股池的交易日（无则 None）。"""
    dates = db.pool_dates()
    return dates[0] if dates else None


def _reg_context(provider: CompositeProvider) -> tuple[dict, set]:
    """盘前监管上下文：连板字典(实时hub·盘前多为空·优雅降级) + 当前停牌集(Tushare·盘前即有)。"""
    tech: dict = {}
    try:
        from app.strategy.realtime_hub import tech_map
        tech = tech_map() or {}
    except Exception as e:
        logger.debug("[盘前体检] tech_map 不可用(盘前正常): %s", e)
    halted: set = set()
    try:
        from app.strategy.reg_risk import suspended_codes
        halted = suspended_codes(provider)
    except Exception as e:
        logger.debug("[盘前体检] 停牌集获取失败: %s", e)
    return tech, halted


def _reg_for(ts: str, name: str, tech: dict, halted: set) -> dict | None:
    """单只监管标记：停牌(事实·优先) > 连板异动核查(派生)。供盘前避雷·无风险返回 None。"""
    if ts in halted:
        return {"kind": "suspend", "level": "high", "text": "停牌中"}
    consec = (tech.get(ts) or {}).get("consec_limit_now")
    from app.strategy.reg_risk import anomaly_risk
    a = anomaly_risk(consec, is_st="ST" in str(name).upper())
    return {"kind": "anomaly", "level": a["level"], "text": a["text"]} if a else None


def check_pool(td: str | None = None, provider: CompositeProvider | None = None,
               *, with_alert: bool = True) -> dict:
    """对选股池逐只做消息面体检。返回结构化结果（不落盘/不推送·便于测试与复用）。"""
    provider = provider or CompositeProvider()
    td = td or _latest_pool_date()
    if not td:
        return {"ok": False, "msg": "暂无选股池", "td": None, "rows": []}

    from app.strategy.fundamentals import get_financials, get_recent_alert

    tech, halted = _reg_context(provider)            # 盘前停牌集 + 连板字典（取数一次·全池复用）
    pool = db.get_pool_with_perf(td)
    rows: list[dict] = []
    for i, r in enumerate(pool):
        ts, name = r["ts_code"], r.get("name", "")
        cls = {"verdict": "中性", "ups": [], "downs": []}
        try:
            fin = get_financials(ts, provider)
            cls = _classify_events(fin.get("events"), fin.get("forecast"))
        except Exception as e:                      # 单只失败不拖垮整体
            logger.debug("[盘前体检] %s 事件取数失败: %s", ts, e)

        alert, sources = "", []
        if with_alert and i < _ALERT_CAP:
            try:
                a = get_recent_alert(ts, name, provider)
                if a.get("ok"):
                    alert, sources = a.get("summary", ""), a.get("sources", [])
            except Exception as e:
                logger.debug("[盘前体检] %s 博查舆情失败: %s", ts, e)

        rows.append({
            "ts_code": ts, "code6": ts.split(".")[0], "name": name,
            "theme": r.get("theme", ""), "is_focus": bool(r.get("is_focus")),
            "confidence": r.get("confidence"), "alert": alert, "sources": sources,
            "reg": _reg_for(ts, name, tech, halted), **cls,
        })

    # 混合票同时进利空/利好两张表（各只显本方向信号）；中性单独计数
    downs = [x for x in rows if x["verdict"] in ("利空", "混合")]
    ups = [x for x in rows if x["verdict"] in ("利好", "混合")]
    neutral_n = sum(1 for x in rows if x["verdict"] == "中性")
    regs = [x for x in rows if x.get("reg")]        # 停牌/连板异动核查（避雷·最优先呈现）
    return {"ok": True, "td": td, "n": len(rows), "rows": rows,
            "downs": downs, "ups": ups, "neutral_n": neutral_n, "regs": regs}


def _fmt_d(d: str) -> str:
    d = str(d or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d


def render_md(result: dict, now_str: str) -> str:
    """体检结果 → Markdown 报告正文。"""
    td = _fmt_d(result.get("td"))
    head = (f"# 📋 选股池盘前消息面体检\n"
            f"> 📅 **{now_str}** ｜ 基于选股池 **{td}**（{result.get('n', 0)}只）"
            f" ｜ 体检隔夜/周末新公告·带来源·**不预测涨跌、不构成建议**\n\n")

    def _sig_table(items: list[dict], direction: str) -> str:
        """direction: 'down'=利空表 / 'up'=利好表。每张表只显本方向信号；混合票打 ⚖️。"""
        label = "利空信号" if direction == "down" else "利好信号"
        lines = [f"| 股票 | 板块 | {label} | 来源 |", "|---|---|---|---|"]
        for x in items:
            sigs = x["downs"] if direction == "down" else x["ups"]
            txt = "；".join(s["text"] for s in sigs)
            src = "、".join(sorted({s["src"] for s in sigs}))
            mark = ("⭐" if x["is_focus"] else "") + ("⚖️" if x["verdict"] == "混合" else "")
            lines.append(f"| {x['name']}({x['code6']}){mark} | {x['theme']} | {txt} | {src} |")
        return "\n".join(lines) + "\n\n"

    body = head
    regs = result.get("regs", [])
    if regs:
        body += (f"## 🔒 停牌 / 监管核查（{len(regs)}只·避雷优先）\n\n"
                 "<small>停牌=交易所事实 ｜ 连板异动=核查风险派生(非确定停牌)</small>\n\n"
                 "| 股票 | 板块 | 风险 |\n|---|---|---|\n")
        for x in regs:
            icon = "🔒停牌" if x["reg"]["kind"] == "suspend" else "⚠️异动核查"
            mark = "⭐" if x["is_focus"] else ""
            body += f"| {x['name']}({x['code6']}){mark} | {x['theme']} | {icon}·{x['reg']['text']} |\n"
        body += "\n"

    downs, ups = result.get("downs", []), result.get("ups", [])
    if downs:
        body += (f"## ⚠️ 利空 / 需留意（{len(downs)}只）\n\n"
                 "<small>⭐=最关注 ⚖️=同时另有利好(混合·两面看)</small>\n\n" + _sig_table(downs, "down"))
    if ups:
        body += f"## ✅ 利好催化（{len(ups)}只）\n\n" + _sig_table(ups, "up")
    if not downs and not ups:
        body += "## ✅ 全池暂无结构化利好/利空公告\n\n所有池内个股近期无减持/解禁/预告/快报/大宗/回购等结构化信号。\n\n"

    # 博查舆情（带来源·只列有内容的，重点票优先）
    with_news = [x for x in result.get("rows", []) if x.get("alert")]
    if with_news:
        with_news.sort(key=lambda x: (not x["is_focus"]))
        body += "## 📰 个股近期消息（博查联网·带来源·仅供研判）\n\n"
        for x in with_news:
            mark = " ⭐最关注" if x["is_focus"] else ""
            body += f"**{x['name']}（{x['code6']}）**{mark}\n\n{x['alert']}\n\n"
            if x.get("sources"):
                links = " · ".join(f"[{s.get('site') or '来源'} {str(s.get('date'))[:10]}]({s.get('url')})"
                                   for s in x["sources"][:5] if s.get("url"))
                if links:
                    body += f"<small>📎 {links}</small>\n\n"
    body += f"\n> 体检 {result.get('neutral_n', 0)} 只无结构化异动。判定仅据公告口径，舆情供研判，不构成投资建议。\n"
    return body


def _push_summary(result: dict) -> None:
    """盘前 Bark 提醒：利空优先（避雷·穿透），利好其次。"""
    from app.notify.notifier import push_bark
    downs, ups = result.get("downs", []), result.get("ups", [])
    regs = result.get("regs", [])
    if not downs and not ups and not regs:
        return                                       # 无异动不打扰
    blocks = [f"选股池 {result['n']}只·{_fmt_d(result['td'])}"]
    if regs:
        names = "、".join(f"{x['name']}({'停牌' if x['reg']['kind'] == 'suspend' else '异动核查'})"
                         for x in regs[:4])
        blocks.append(f"🔒停牌/核查{len(regs)}: {names}")
    if downs:
        names = "、".join(f"{x['name']}({x['downs'][0]['text'] if x['downs'] else '混合'})" for x in downs[:4])
        blocks.append(f"⚠️利空{len(downs)}: {names}")
    if ups:
        names = "、".join(f"{x['name']}({x['ups'][0]['text']})" for x in ups[:4])
        blocks.append(f"✅利好{len(ups)}: {names}")
    blocks.append(f"余{result.get('neutral_n', 0)}只无异动")
    push_bark(title=f"📋 选股池盘前体检·{result['n']}只", body=" ▸ ".join(blocks),
              group="选股池", level="timeSensitive" if (downs or regs) else "active")


def run_pool_check(*, push: bool = True, provider: CompositeProvider | None = None) -> tuple[str, str, str] | None:
    """编排：体检 → 写报告(进报告中心) →（可选）盘前 Bark。返回 (filepath, title, content) 或 None。"""
    result = check_pool(provider=provider)
    if not result.get("ok"):
        logger.info("[盘前体检] 跳过：%s", result.get("msg"))
        return None

    now = datetime.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    title = "选股池盘前消息面体检"
    content = render_md(result, now_str)            # 已含 # 标题

    settings = get_settings()
    filepath = settings.report_dir / f"{now.strftime('%Y%m%d')}_{now.strftime('%H%M')}_poolcheck.md"
    filepath.write_text(content, encoding="utf-8")

    if push:
        try:
            _push_summary(result)
        except Exception as e:
            logger.warning("[盘前体检] Bark 推送失败: %s", e)
    logger.info("[盘前体检] 完成：%s 利空%d/利好%d/中性%d",
                result["td"], len(result["downs"]), len(result["ups"]), result["neutral_n"])
    return str(filepath), title, content
