"""
🤖 AI 投研问答 Agent：给 LLM 一套"金融工具"，它据问题**自己去调真实数据**（行情/财务/事件/研报/新闻/
信号/板块/持仓），再基于真数据回答 —— 比外面没接金融数据的 LLM 强在"有据可查、不瞎编"。

设计：
  - 工具轮（非流式·需完整 tool_calls）：LLM 决定调哪些工具 → 执行(复用现有函数) → 喂回 → 直到不再调工具。
  - 最终答案：流式输出（打字机）。
  - run_chat 是生成器，逐步 yield 事件：status(查询中) / thinking(思考) / delta(正文流) / done。

红线（CLAUDE.md）：只用工具返回的真实数据、标出处、缺数据明说、**不预测涨跌、不输出胜率、不荐买卖**。
"""

from __future__ import annotations

import json
import logging

from app.data.composite_provider import CompositeProvider
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 5
_AGENT_TASK = "pro"     # 工具选择+综合用强模型

_SYSTEM = (
    "你是「A股投研助手」，已接入真实金融数据工具。准则：\n"
    "1. 回答个股/板块/大盘/持仓相关问题，**必须先用工具查真实数据**，基于工具返回结果作答，"
    "严禁编造价格/数字/事件/研报；工具没有的信息明说「暂无数据」，不要瞎补。\n"
    "2. 标注数据出处（如：据财报/东财研报/同花顺一致预期/博查新闻/你的持仓体检）。\n"
    "3. **绝不预测涨跌、不输出胜率或「必涨」、不给买入/卖出指令**；可客观陈述数据与信号、提示风险，让用户自己决策。\n"
    "4. 问「我的持仓/我的票」用 my_portfolio 工具。问某只票综合情况，可组合调用 行情+财务+研报+新闻。\n"
    "5. 用清晰中文，重点可分点；简洁不啰嗦。"
)


# ──────────────────────────────────────────────────────────────────────────
# 工具定义（OpenAI function schema）
# ──────────────────────────────────────────────────────────────────────────

def _tool_schemas() -> list[dict]:
    stock = {"type": "object", "properties": {"stock": {"type": "string", "description": "股票名称或代码，如 中际旭创 或 300308"}}, "required": ["stock"]}
    return [
        _fn("stock_quote", "查个股最新价/涨跌幅/所属行业赛道（快）", stock),
        _fn("stock_financials", "查个股财报趋势(净利同比/ROE/营收/负债)+业绩预告+事件避雷(解禁/减持/大宗/快报/户数)", stock),
        _fn("stock_research", "查个股券商研报：东财(评级分布/盈利预测增速/PDF) + 同花顺一致预期(机构数/分年EPS/行业平均)", stock),
        _fn("stock_news", "查个股近一月真实新闻要点(博查联网·业绩/订单/扩产/减持/政策等)", stock),
        _fn("stock_best_signal", "查个股历史上最吃哪种短线打法/信号(确定性回测·非预测)", stock),
        _fn("sector_heat", "查某板块/概念的热度+主力资金+阶段", {"type": "object", "properties": {"name": {"type": "string", "description": "同花顺概念或申万行业名，如 共封装光学(CPO) / 半导体"}}, "required": ["name"]}),
        _fn("my_portfolio", "查用户自己的自选/持仓：盈亏+持仓体检(健康灯)+事件预警", {"type": "object", "properties": {}}),
        _fn("search_news", "联网搜索任意财经主题的真实新闻(博查)，如政策/行业/事件", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
    ]


def _fn(name: str, desc: str, params: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


_LABELS = {
    "stock_quote": "查行情", "stock_financials": "查财报/事件", "stock_research": "查研报",
    "stock_news": "查新闻", "stock_best_signal": "查最佳信号", "sector_heat": "查板块热度",
    "my_portfolio": "查我的持仓", "search_news": "联网搜索",
}


def _tool_status(name: str, args: dict) -> str:
    arg = args.get("stock") or args.get("name") or args.get("query") or ""
    return f"🔧 {_LABELS.get(name, name)}{('：' + arg) if arg else ''}…"


# ──────────────────────────────────────────────────────────────────────────
# 工具执行（复用现有函数·返回紧凑 dict）
# ──────────────────────────────────────────────────────────────────────────

def _resolve(stock: str, provider: CompositeProvider) -> tuple[str, str]:
    """名称/代码 → (ts_code, name)。"""
    s = (stock or "").strip()
    try:
        sb = provider.get_stock_basic()
    except Exception:
        return "", ""
    code6 = s.split(".")[0]
    if code6.isdigit():
        hit = sb[sb["ts_code"].str.startswith(code6)]
    else:
        hit = sb[sb["name"].astype(str) == s]
        if hit.empty:
            hit = sb[sb["name"].astype(str).str.contains(s, na=False)]
    if hit.empty:
        return "", ""
    r = hit.iloc[0]
    return str(r["ts_code"]), str(r["name"])


def _exec_tool(name: str, args: dict, provider: CompositeProvider) -> dict:
    try:
        return _TOOLS[name](args, provider)
    except Exception as e:
        logger.exception("[chat] 工具 %s 执行失败", name)
        return {"error": f"{name} 执行失败：{str(e)[:80]}"}


def _t_quote(args, provider) -> dict:
    ts, name = _resolve(args.get("stock", ""), provider)
    if not ts:
        return {"error": f"未找到股票「{args.get('stock')}」"}
    out = {"代码": ts, "名称": name}
    try:
        q = provider.get_realtime_quote([ts])
        if q is not None and not q.empty:
            r = q.iloc[0]
            out["现价"] = round(float(r["price"]), 2)
            out["涨跌幅%"] = round(float(r["pct_chg"]), 2)
    except Exception:
        pass
    try:
        sb = provider.get_stock_basic()
        h = sb[sb["ts_code"] == ts]
        if not h.empty:
            out["行业"] = str(h.iloc[0]["industry"])
    except Exception:
        pass
    return out


def _t_financials(args, provider) -> dict:
    ts, name = _resolve(args.get("stock", ""), provider)
    if not ts:
        return {"error": f"未找到股票「{args.get('stock')}」"}
    from app.strategy.fundamentals import get_financials
    f = get_financials(ts, provider)
    if not f.get("ok"):
        return {"名称": name, "财务": f.get("msg", "暂无财务数据")}
    out = {"名称": name, "财务摘要": f.get("summary"), "最新期": f.get("latest_period")}
    if f.get("forecast"):
        fc = f["forecast"]
        out["业绩预告"] = f"{fc.get('type')} {fc.get('net_change') or ''}".strip()
    ev = f.get("events") or {}
    risks = []
    if ev.get("float"):
        fl = ev["float"]; risks.append(f"解禁 {fl.get('next_date')}(距{fl.get('next_days')}天·{fl.get('next_ratio')}%)")
    if ev.get("holder_trade"):
        ht = ev["holder_trade"]; risks.append(f"减持{ht.get('de_count')}/增持{ht.get('in_count')}次")
    if ev.get("block"):
        bl = ev["block"]; risks.append(f"大宗{bl.get('count')}笔折溢价{bl.get('premium_avg')}%")
    if ev.get("express"):
        risks.append(f"快报净利同比{ev['express'].get('net_profit_yoy')}%")
    if risks:
        out["事件避雷"] = risks
    return out


def _t_research(args, provider) -> dict:
    ts, name = _resolve(args.get("stock", ""), provider)
    if not ts:
        return {"error": f"未找到股票「{args.get('stock')}」"}
    from app.strategy.fundamentals import get_em_research, get_ths_forecast
    out = {"名称": name}
    em = get_em_research(ts, provider)
    if em.get("ok"):
        out["东财研报"] = {"近半年机构数": em["n_org"], "篇数": em["n_reports"], "评级": em["ratings"],
                       "买入占比%": em["buy_ratio"], "盈利预测增速%": em.get("eps_growth")}
    ths = get_ths_forecast(ts, provider)
    if ths.get("ok"):
        out["同花顺一致预期"] = {"机构数": ths["max_n_org"],
                          "分年EPS均值": {y["year"]: y["eps_avg"] for y in ths["by_year"]},
                          "隐含增速%": ths.get("eps_growth"), "行业平均EPS": ths.get("ind_avg")}
    return out or {"名称": name, "研报": "暂无"}


def _t_news(args, provider) -> dict:
    ts, name = _resolve(args.get("stock", ""), provider)
    if not ts:
        return {"error": f"未找到股票「{args.get('stock')}」"}
    from app.strategy.fundamentals import get_recent_alert
    a = get_recent_alert(ts, name, provider)
    if not a.get("ok"):
        return {"名称": name, "新闻": a.get("msg", "暂无")}
    return {"名称": name, "近期要点": a.get("summary"),
            "来源": [f"{s.get('site')} {s.get('date')}" for s in (a.get("sources") or [])[:5]]}


def _t_best_signal(args, provider) -> dict:
    ts, name = _resolve(args.get("stock", ""), provider)
    if not ts:
        return {"error": f"未找到股票「{args.get('stock')}」"}
    import datetime
    from app.backtest.strategy_scout import scout_strategies
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=95)).strftime("%Y%m%d")
    r = scout_strategies(ts, start, end, provider, name=name)
    if not r.get("ok"):
        return {"名称": name, "信号": r.get("msg", "数据不足")}
    top = [s for s in r.get("ranked", []) if s.get("tier") in ("rec", "rec_thin")][:3]
    return {"名称": name, "窗口": r.get("window_label"),
            "最吃的打法": [{"信号": s["label"], "类别": s.get("category"), "T+5期望%": s["avg_return"],
                       "胜率%": round(s["win_rate"] * 100), "样本n": s["n"]} for s in top],
            "说明": "确定性历史统计·历史≠未来·非预测"}


def _t_sector(args, provider) -> dict:
    from app.data.theme_heat_db import get_theme, latest_trade_date
    name = (args.get("name") or "").strip()
    for typ in ("concept", "industry"):
        d = latest_trade_date(typ)
        row = get_theme(d, name, typ) if d else None
        if row:
            return {"板块": name, "类型": "概念" if typ == "concept" else "行业",
                    "热度": row.get("heat_score"), "3日资金(亿)": row.get("money_flow_3d"),
                    "3日涨跌%": row.get("pct_chg_3d"), "阶段": row.get("phase"), "数据日": row.get("trade_date")}
    return {"板块": name, "提示": "未找到该板块（请用同花顺概念名或申万行业名）"}


def _t_portfolio(args, provider) -> dict:
    from app.strategy.portfolio import build_portfolio
    p = build_portfolio(provider)
    if not p.get("rows"):
        return {"持仓": "用户暂无自选/持仓"}
    rows = [{"名称": r["name"], "持仓": r["is_holding"], "现价": r.get("price"),
             "盈亏%": r.get("pnl"), "健康灯": r.get("health"),
             "触发": [f["text"] for f in r.get("flags", [])]} for r in p["rows"]]
    return {"共": p["summary"]["n"], "持仓数": p["summary"]["n_holding"],
            "总浮盈%": p["summary"].get("total_pnl"), "明细": rows,
            "预警": [f"{a['name']}：{a['text']}" for a in p.get("alerts", [])]}


def _t_search(args, provider) -> dict:
    from app.strategy.detail_common import web_search
    res = web_search(args.get("query", ""))
    if not res:
        return {"结果": "未检索到（博查未配置或无结果）"}
    return {"新闻": [{"标题": w.get("title"), "来源": w.get("site"), "日期": w.get("date"),
                    "摘要": (w.get("summary") or w.get("snippet") or "")[:120]} for w in res[:6]]}


_TOOLS = {
    "stock_quote": _t_quote, "stock_financials": _t_financials, "stock_research": _t_research,
    "stock_news": _t_news, "stock_best_signal": _t_best_signal, "sector_heat": _t_sector,
    "my_portfolio": _t_portfolio, "search_news": _t_search,
}


# ──────────────────────────────────────────────────────────────────────────
# Agent 主循环（生成器·逐步 yield 事件）
# ──────────────────────────────────────────────────────────────────────────

def run_chat(history: list[dict], provider: CompositeProvider | None = None, client=None):
    """
    history: [{role:'user'/'assistant', content}]（含最新用户消息）。
    yield 事件 dict：{type:'status'|'thinking'|'delta'|'done'|'error', ...}。
    """
    provider = provider or CompositeProvider()
    client = client or LLMClient()
    messages = [{"role": "system", "content": _SYSTEM}, *history]
    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            msg = client.complete_with_tools(messages, _tool_schemas(), task_type=_AGENT_TASK)
            if not getattr(msg, "tool_calls", None):
                break
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [_tc_dict(tc) for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                args = _safe_args(tc.function.arguments)
                yield {"type": "status", "text": _tool_status(tc.function.name, args)}
                result = _exec_tool(tc.function.name, args, provider)
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
        # 最终答案：流式（无工具→只答）
        parts, thinking_sent = [], False
        for kind, text in client.stream_answer(messages, task_type=_AGENT_TASK):
            if kind == "reasoning":
                if not thinking_sent:
                    yield {"type": "thinking", "text": "💭 思考中…"}
                    thinking_sent = True
            else:
                parts.append(text)
                yield {"type": "delta", "text": text}
        yield {"type": "done", "content": "".join(parts)}
    except Exception as e:
        logger.exception("[chat] Agent 运行失败")
        yield {"type": "error", "text": f"出错了：{str(e)[:120]}"}


def _tc_dict(tc) -> dict:
    return {"id": tc.id, "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}


def _safe_args(raw: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}
