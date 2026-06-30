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
import re

from app.data.composite_provider import CompositeProvider
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 8    # 多股研究易超 5 轮被打断→末轮流式吐工具token；放宽让模型查完再答
_AGENT_TASK = "pro"     # 工具选择+综合用强模型
# 安全网：推理模型偶尔把工具调用 token 当文本吐出(<｜tool_calls｜>/DSML/invoke name=...)，检测到即截断不展示
_TOOL_LEAK_RE = re.compile(r"DSML|tool[▁_]?calls?|<\s*[|｜]\s*tool|invoke\s+name\s*=", re.I)

_SYSTEM = (
    "你是**资深 A股投研分析师**，已接入真实金融数据工具。目标是给出有洞察、有观点、**敢下判断**的分析，"
    "而不是把数据一摆就让用户自己猜。\n\n"
    "【先查真数据】回答个股/板块/大盘/持仓相关问题，先用工具查真实数据再分析；问「我的持仓/我的票」用 my_portfolio；"
    "问「机构在买什么/机构在卖什么/龙虎榜机构动向」用 inst_lhb_board（A股仅存的个股级真机构钱·真金白银）；"
    "问某只票综合情况可组合调 行情+财务+研报+新闻。工具没有的明说「暂无数据」，**绝不编造价格/数字/事件/研报**。\n\n"
    "【要给判断·别和稀泥】\n"
    "- 该下结论就下结论：**谁是龙头、谁更强、估值贵不贵、逻辑强不强、风险在哪**——给出你明确的倾向和理由，别把判断全推给用户。\n"
    "- 可以横向对比、排序（按基本面/机构覆盖/成长性/估值/资金等**真实维度**）、点名你认为的龙头并说依据。\n"
    "- 可以评价估值是否合理（结合 PE/一致预期增速/行业对比）、给关注点、说明加仓/减仓/止损的参考逻辑与触发条件。\n\n"
    "【数据必须可溯源·铁律·不可违反】\n"
    "- 你引用的**每一个数字/结论**，必须用工具返回里**自带的来源与日期**说清出处；工具结果里**没有标注来源或日期的数字，一律当「未核实」**，"
    "必须明说「这个数我没核到出处、建议你核对公告」，**绝不当作确定事实去下判断**。\n"
    "- 业绩预告、财报、龙虎榜这类硬数据，引用时必须给出**报告期/数据日 + 公告日 + 工具提供的核查链接**，让用户能去复查原文。"
    "如 stock_financials 返回的「业绩预告」对象，要把其中 报告期/公告日/核查链接 一并写给用户。\n"
    "- **务必看清时效**：业绩预告是『前瞻』信号，一旦对应报告期的实际财报已出，该预告就过期作废，**不能再当当前业绩**；"
    "看清公告日与报告期，绝不把一年前的旧预告说成最新「暴雷」。两个数字打架时（如预告 vs 实际财报），以**更新、更接近实际**的为准并点明。\n"
    "- 分清数据性质：**交易所披露的硬数据(可核查) vs 估算/代理口径/联网新闻(需自行甄别)**，分别标清，不可混为一谈。\n"
    "- **现价与今日涨跌幅**：只能用 stock_quote(新浪实时·带『数据时间』)当轮返回的值；**没调 stock_quote 就绝不报现价/涨幅**，"
    "**更绝不用你训练记忆里的股价**——你记忆里的价往往过时几个月到几年（曾把现价 74 元说成 28 元），一律不可信。\n"
    "- **PE/目标价/评级/一致预期**来自研报，**有日期、可能滞后数月**，引用必须标『研报数据·截至X日』、**不得称『实时』**；"
    "**工具没返回的数字（如目标价为空）绝不编造一个**。谈估值/上涨空间，必须用 stock_quote 的**实时现价**去对研报目标价/EPS——"
    "若现价已远超研报目标价，要老实说『股价已透支研报预期』，**绝不能用旧价硬说『便宜/有空间』**。\n"
    "- **任何派生指标**（如『近20日涨X%』『区间涨幅』）工具没明确返回就**不许说**，不准自己脑补编算。\n"
    "- 宁可说「这个我没核到来源，需你复查」，也**绝不臆想、绝不把没出处的结论当事实**——这是反复出错的根因，务必杜绝。\n\n"
    "【底线·这不是和稀泥而是诚实】\n"
    "- 判断要讲依据，并说清这是「基于现有数据的分析观点」，不是确定性保证。\n"
    "- 不打包票保证涨跌幅/收益率、不说「必涨/稳赚/一定」；**不编造或臆测胜率数字**。\n"
    "- 给的是参考逻辑，最终下单与仓位由用户定、风险自担；机会与风险两面都讲，不只报喜。\n\n"
    "用清晰中文、有条理、直接切要点，像个能给真知灼见的分析师，**不要当免责声明机器**。"
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
        _fn("inst_lhb_board", "查最近交易日龙虎榜机构席位真实净买/净卖榜（真金白银·A股仅存的个股级真机构钱）", {"type": "object", "properties": {"tech_only": {"type": "boolean", "description": "是否只看科技赛道(电子/通信/计算机/半导体等)，默认否"}}}),
        _fn("market_overview", "查大盘体检：当前状态(强/震/弱)+上证点位+成交量水位+市场广度+涨停跌停+连板高度+情绪温度+近5日领涨/领跌板块（做『大盘格局/后市/择时/仓位』判断必调）", {"type": "object", "properties": {}}),
    ]


def _fn(name: str, desc: str, params: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


_LABELS = {
    "stock_quote": "查行情", "stock_financials": "查财报/事件", "stock_research": "查研报",
    "stock_news": "查新闻", "stock_best_signal": "查最佳信号", "sector_heat": "查板块热度",
    "my_portfolio": "查我的持仓", "search_news": "联网搜索", "inst_lhb_board": "查机构动向",
    "market_overview": "查大盘体检",
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


def _fill_quote_fullpush(ts: str, out: dict) -> bool:
    """盘中用进程内全推L1快照填实时报价(现价/涨跌/量比/内外盘)。非实时/无该票返回 False。"""
    try:
        from app.strategy import realtime_hub as hub
        if not hub.is_live():
            return False
        q = hub.snapshot().get(ts)
        if not q or float(q.get("price") or 0) <= 0:
            return False
        out["现价"] = round(float(q.get("price") or 0), 2)
        out["涨跌幅%"] = round(float(q.get("pct_chg") or 0), 2)
        if q.get("prev_close"):
            out["昨收"] = round(float(q["prev_close"]), 2)
        if q.get("vol_ratio"):
            out["量比"] = round(float(q["vol_ratio"]), 2)
        inn, outr = float(q.get("inner") or 0), float(q.get("outer") or 0)
        if inn + outr > 0:
            out["外盘占比%"] = round(outr / (inn + outr) * 100, 1)   # 主动买占比·>50偏主动买
        out["数据时间"] = hub._as_of_str()
        out["来源"] = "幕数据沪深全推L1·盘中秒级实时(现价/涨跌/量比/内外盘)；谈现价一律用它，绝不用记忆里的旧价"
        return True
    except Exception:
        return False


def _t_quote(args, provider) -> dict:
    import datetime
    ts, name = _resolve(args.get("stock", ""), provider)
    if not ts:
        return {"error": f"未找到股票「{args.get('stock')}」"}
    out = {"代码": ts, "名称": name}
    if not _fill_quote_fullpush(ts, out):        # ① 优先全推L1(盘中秒级·含量比/内外盘)
        try:                                     # ② 全推休市/断流/无该票 → 新浪兜底
            q = provider.get_realtime_quote([ts])
            if q is not None and not q.empty:
                r = q.iloc[0]
                out["现价"] = round(float(r["price"]), 2)
                out["涨跌幅%"] = round(float(r["pct_chg"]), 2)
                try:
                    out["昨收"] = round(float(r.get("prev_close")), 2)
                except (TypeError, ValueError):
                    pass
                out["数据时间"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                out["来源"] = "新浪实时行情·权威当前价；谈现价/涨幅/估值空间一律用它，绝不用记忆里的旧价"
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
    out = {"名称": name, "财务摘要": f.get("summary"), "最新期": f.get("latest_period"),
           "财务来源": "Tushare fina_indicator(交易所定期报告口径)"}
    if f.get("forecast"):
        fc = f["forecast"]
        # 带全溯源：报告期+公告日+摘要+来源+核查链接，且只会是「仍前瞻」的预告（过期的已在数据层剔除）
        out["业绩预告"] = {
            "结论": f"{fc.get('type')} {fc.get('net_change') or ''}".strip(),
            "报告期": fc.get("period"),
            "公告日": fc.get("ann_date"),
            "摘要": fc.get("summary") or "",
            "来源": fc.get("source"),
            "核查链接": fc.get("verify_url"),
        }
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
        out["东财研报"] = {"性质": "券商研报口径·非实时·可能滞后数月",
                       "最新报告日": em.get("latest"),
                       "近半年机构数": em["n_org"], "篇数": em["n_reports"], "评级": em["ratings"],
                       "买入占比%": em["buy_ratio"], "盈利预测增速%": em.get("eps_growth"),
                       "说明": "本工具未提供个股目标价；要谈估值/上涨空间，请用 stock_quote 的实时现价去对比，切勿编造目标价"}
    ths = get_ths_forecast(ts, provider)
    if ths.get("ok"):
        out["同花顺一致预期"] = {"性质": "分析师一致预测(EPS)·非实时行情",
                          "机构数": ths["max_n_org"],
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


def _t_inst_board(args, provider) -> dict:
    """龙虎榜机构净买/净卖榜：当日真机构钱。返回紧凑买/卖各前 10。"""
    from datetime import datetime

    from app.factors.breadth_qfq import _recent_trade_dates
    from app.strategy.lhb_inst import build_inst_board
    today = datetime.now().strftime("%Y%m%d")
    try:
        date = _recent_trade_dates(provider, today, 1)[-1]
    except Exception:
        date = today
    tech_only = bool(args.get("tech_only"))
    b = build_inst_board(provider, date, top=10, tech_only=tech_only)

    def _fmt(f):
        return {"名称": f["name"], "行业": f["industry"], "机构净额(亿)": f["net_yi"],
                "席位": f["seats"], "上榜原因": (f.get("reason") or "")[:24]}
    return {"交易日": b["date"], "口径": "科技赛道" if tech_only else "全市场",
            "上榜机构票数": b["n_total"],
            "机构净买Top": [_fmt(f) for f in b["buys"]],
            "机构净卖Top": [_fmt(f) for f in b["sells"]],
            "说明": "龙虎榜机构专用席位=真金白银·仅当日异动股·净买≠必涨·需结合基本面"}


def _t_market_overview(args, provider) -> dict:
    """大盘体检：当前状态/上证点位/成交量水位/广度/涨停跌停/情绪/近5日领涨跌板块。给"大盘格局/后市"判断打底。"""
    from datetime import datetime

    from app.strategy.market_overview import build_overview
    d = build_overview(datetime.now().strftime("%Y%m%d"), 20)
    k = d.get("kpi", {})
    r = d.get("regime_now", {})
    lv = [x for x in (d.get("index_level") or []) if x is not None]
    ic = d.get("index_cum") or []
    day_pct = (round(ic[-1] - ic[-2], 2)
               if len(ic) >= 2 and ic[-1] is not None and ic[-2] is not None else None)
    s = d.get("sectors", {})
    names, mat, dts = s.get("names", []), s.get("matrix", []), s.get("dates", [])
    sums = []
    for i, nm in enumerate(names):
        vals = [mat[i][j] for j in range(max(0, len(dts) - 5), len(dts)) if mat[i][j] is not None]
        if vals:
            sums.append((nm, round(sum(vals), 1)))
    sums.sort(key=lambda x: -x[1])
    return {
        "交易日": d.get("end_date"), "状态": r.get("label"), "诊断": r.get("reason"),
        "上证点位": lv[-1] if lv else None, "上证今日%": day_pct,
        "成交额(万亿)": k.get("amount_wy"), "成交额较前日(亿)": k.get("amount_chg_yi"),
        "成交额水位(分位·0地量100天量)": d.get("amount_pct"),
        "涨停": k.get("limit_up"), "跌停": k.get("limit_down"), "最高连板": k.get("lianban_height"),
        "情绪温度(0冰100热)": k.get("temp"), "涨家数": k.get("up_count"), "跌家数": k.get("down_count"),
        "当前地量+广度冰点": d.get("dryup_now"),
        "近5日领涨板块": [f"{n} {'+' if v >= 0 else ''}{v}%" for n, v in sums[:6]],
        "近5日领跌板块": [f"{n} {'+' if v >= 0 else ''}{v}%" for n, v in sums[-6:][::-1]],
        "说明": "状态/阈值经验派生·非保证；不预测点位、不构成建议",
    }


_TOOLS = {
    "stock_quote": _t_quote, "stock_financials": _t_financials, "stock_research": _t_research,
    "stock_news": _t_news, "stock_best_signal": _t_best_signal, "sector_heat": _t_sector,
    "my_portfolio": _t_portfolio, "search_news": _t_search, "inst_lhb_board": _t_inst_board,
    "market_overview": _t_market_overview,
}


# ──────────────────────────────────────────────────────────────────────────
# Agent 主循环（生成器·逐步 yield 事件）
# ──────────────────────────────────────────────────────────────────────────

def _now_context() -> str:
    """当前时间 + 交易状态 + 最近交易日（每次对话注入系统提示·让 AI 立足"现在"，不用训练里的旧日期）。"""
    import datetime

    from app.strategy.realtime_hub import market_session
    from app.strategy.trade_calendar import is_trading_day, last_trading_day
    now = datetime.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    today, hm = now.strftime("%Y%m%d"), now.strftime("%H%M")
    sess = market_session()
    if not is_trading_day():
        state = "今日非交易日(周末/节假日·A股休市)"
    elif sess in ("auction", "auction_lock", "pre_open"):
        state = "今日交易日·集合竞价/盘前(9:15-9:30)"
    elif sess == "continuous":
        state = "今日交易日·A股连续交易中"
    elif hm < "0915":
        state = "今日交易日·盘前未开盘"
    elif "1130" <= hm < "1300":
        state = "今日交易日·午间休市"
    else:
        state = "今日交易日·已收盘(15:00后)"
    last_td = last_trading_day(today) or today
    last_fmt = f"{last_td[:4]}-{last_td[4:6]}-{last_td[6:]}" if len(last_td) == 8 else last_td
    return (f"【当前时间】{now.strftime('%Y-%m-%d %H:%M')} 周{wd}（{state}）。"
            f"最近交易日 {last_fmt}（行情/资金类数据若工具未返回更新，即截至该日）。"
            f"凡涉及'现在/今天/最近/最新/目前/这周'，**一律以上述时间为准，绝不使用你训练知识里的旧日期**；"
            f"不确定具体日期就用工具查或明说不确定。")


def run_chat(history: list[dict], provider: CompositeProvider | None = None, client=None,
             task: str = _AGENT_TASK):
    """
    history: [{role:'user'/'assistant', content}]（含最新用户消息）。
    task: 模型档位——'pro'(强·复杂推演) / 'flash'(快省·简单查询)。默认 pro。
    yield 事件 dict：{type:'status'|'thinking'|'delta'|'done'|'error', ...}。
    """
    task = task if task in ("pro", "flash") else _AGENT_TASK
    provider = provider or CompositeProvider()
    client = client or LLMClient()
    messages = [{"role": "system", "content": _SYSTEM + "\n\n" + _now_context()}, *history]
    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            msg = client.complete_with_tools(messages, _tool_schemas(), task_type=task)
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
        # 最终答案：流式。传 tools + tool_choice="none"(API层禁止再调工具) + 泄漏检测(双保险)
        parts, thinking_sent, stopped = [], False, False
        for kind, text in client.stream_answer(messages, task_type=task,
                                                tools=_tool_schemas()):
            if kind == "reasoning":
                if not thinking_sent:
                    yield {"type": "thinking", "text": "💭 思考中…"}
                    thinking_sent = True
                continue
            if stopped:
                continue
            combined = "".join(parts) + text
            m = _TOOL_LEAK_RE.search(combined)
            if m:                                            # 模型仍吐工具token→只保留标记前正文·停止
                delta = combined[len("".join(parts)):m.start()]
                if delta:
                    parts.append(delta)
                    yield {"type": "delta", "text": delta}
                stopped = True
                continue
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
