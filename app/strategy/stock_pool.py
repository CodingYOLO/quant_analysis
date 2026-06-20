"""
S2：选股池策略引擎（5 内置策略 + 多路命中置信度 + 风控）。

流程：信号表(signals) → 各策略判定 → union → 多路置信度 → 风控 → 推荐池。
策略与置信度口径见《选股池设计文档》§3/§4。数字全规则计算，理由(LLM)在 S5 接入。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.factors.core import calc_position_pct
from app.strategy.signals import build_signal_table

logger = logging.getLogger(__name__)

# 策略键 → 中文名
STRATEGY_NAMES = {
    "breakout": "主升突破", "pullback": "资金回调低吸",
    "flow_follow": "资金跟随", "reversal": "反转低吸", "theme_pick": "主题选股",
}

# 置信度权重（《设计文档》§4.2，梯度标定对齐截图：策略0.75/主题0.46/规则0.37）
_W = {"strategy": 0.35, "theme": 0.15, "rule_selected": 0.08, "factor": 0.30}

_THEME_HEAT_MIN = 60.0       # 主题选股：热度下限
_MAX_PER_THEME = 3           # 单主题入选上限（风控）
_FOCUS_CONF_MIN = 0.55       # 最关注置信度下限

# 历史主题胜率前向闸门（对标吴川：低胜率主题禁追涨）
_WINRATE_MIN_SAMPLES = 10    # 闸门生效的最小历史样本数（不足则不判，避免小样本误杀）
_WINRATE_VETO = 0.35         # T+1 胜率 < 35% 且样本足 → 降级"仅观察"+避免追涨
_WINRATE_WARN = 0.45         # T+1 胜率 < 45% 且样本足 → 仅警示不降级
_FLOW_FOLLOW_TOPN = 6        # 资金跟随：成交额前 N


def build_stock_pool(trade_date: str, provider: CompositeProvider | None = None,
                     market_label: str = "震荡", persist: bool = True) -> list[dict]:
    """
    生成指定交易日的选股池（推荐池）。

    Args:
        trade_date:   交易日 YYYYMMDD
        market_label: 大盘状态（用于仓位与风控；默认震荡）
        persist:      是否落库 + 将最关注写入前向追踪
    Returns:
        list[dict]，每条为一只候选股的完整记录（见《设计文档》§7）。
    """
    provider = provider or CompositeProvider()
    sig = build_signal_table(trade_date, provider)
    if sig.empty:
        logger.warning("[选股池] %s 信号表为空", trade_date)
        return []

    hot_industries = _hot_themes(trade_date)                 # {industry: heat}
    amount_top = set(sig.nlargest(_FLOW_FOLLOW_TOPN, "amount_yi").index)  # 成交额前6
    zhaban_warn = _zhaban_premium_warn(trade_date, provider)  # 昨日涨停溢价率<-1%

    records = []
    for ts, r in sig.iterrows():
        strategies = _match_strategies(r, ts in amount_top, hot_industries)
        if not strategies:
            continue
        rec = _assemble(ts, r, strategies, hot_industries, market_label)
        records.append(rec)

    # 风控：单主题上限 + 涨停溢价预警 + 大盘
    records = _apply_risk(records, market_label, zhaban_warn)
    # 重点分(区分度高·替代饱和置信度) + 星标本池最强 Top5（始终标，供观察名单）
    _compute_focus_scores(records)
    # 质量门槛收紧：只留重点分≥阈值的强票（宁缺毋滥·对标吴川的精选感）
    n_all = len(records)
    records = [r for r in records if r["focus_score"] >= _POOL_MIN_FOCUS]
    # 排序：星标优先 → 重点分；普涨强势日再封顶 _POOL_MAX，避免爆量
    records.sort(key=lambda x: (x["star"], x["focus_score"]), reverse=True)
    records = records[:_POOL_MAX]
    logger.info("[选股池] %s 候选 %d→精选 %d 只（门槛%.0f·封顶%d｜星标 %d / 最关注 %d）", trade_date,
                n_all, len(records), _POOL_MIN_FOCUS, _POOL_MAX,
                sum(r["star"] for r in records), sum(r["is_focus"] for r in records))

    if persist:
        _persist(trade_date, records, market_label)
    return records


def _persist(trade_date: str, records: list[dict], market_label: str) -> None:
    """落库 stock_pool；最关注写入 selection_records 以复用 T+1/3/5 前向回填。"""
    from app.strategy.db import save_pool
    save_pool(trade_date, records)

    focus = [r for r in records if r["is_focus"]]
    if not focus:
        return
    from app.strategy.db import SelectionRecord, save_selections
    recs = [
        SelectionRecord(
            run_date=trade_date, ts_code=r["ts_code"], name=r["name"],
            theme=r["theme"], market_label=market_label, is_backtest=0,
            total_score=r["confidence"] * 100, rps50=r["rps50"],
            main_net_flow=r["main_flow_3d"], change_pct_7d=r["change_7d"],
            entry_price=0.0,   # 次日开盘价待回填
            stop_loss=r["stop_loss"], take_profit_1=r["take_profit_1"], take_profit_2=r["take_profit_2"],
        )
        for r in focus
    ]
    save_selections(recs)
    logger.info("[选股池] %d 只最关注已写入前向追踪", len(focus))


# ──────────────────────────────────────────────
# 策略判定
# ──────────────────────────────────────────────

def _match_strategies(r, in_amount_top: bool, hot: dict) -> list[str]:
    """返回该股命中的策略键列表。"""
    base = bool(r["above_ma20"] and r["above_ma60"] and r["slope_up"])
    out = []
    if base and r["breakout"] and (r["pct_chg"] < 9.5 or r["turnover"] > 3) \
            and r["popular"] and r["main_flow_3d"] > 0:
        out.append("breakout")
    if base and r["is_pullback"] and r["main_flow_3d"] > 0 and r["popular"]:
        out.append("pullback")
    if base and r["is_pullback"] and r["turnover"] > 3 and in_amount_top:
        out.append("flow_follow")
    if base and r["change_7d"] <= 5 and r["main_flow_3d"] >= 0 and r["rev_form"]:
        out.append("reversal")
    # ⑤ 主题选股：属于热门行业主题 + 强势(RPS高/主力流入)
    if hot.get(r["industry"], 0) >= _THEME_HEAT_MIN and r["rps50"] >= 70 and r["main_flow_3d"] > 0:
        out.append("theme_pick")
    return out


def _assemble(ts, r, strategies: list[str], hot: dict, market_label: str) -> dict:
    quant_strats = [s for s in strategies if s != "theme_pick"]
    theme_hit = "theme_pick" in strategies
    rule_selected = bool(r["rps50"] >= 70 and r["main_flow_3d"] > 0)   # 规则已选(强门槛)

    # 置信度（梯度标定，对齐吴川：策略命中权重最高 > 主题 > 规则；因子分微调）
    factor_norm = _factor_score_norm(r)
    conf = (0.10                                          # 入池基底
            + _W["strategy"] * (1 if quant_strats else 0)
            + _W["theme"] * (1 if theme_hit else 0)
            + _W["rule_selected"] * (1 if rule_selected else 0)
            + _W["factor"] * factor_norm)
    conf = round(min(max(conf, 0.0), 0.98), 2)

    # 来源标签
    sources = ["规则候选"]
    if rule_selected:
        sources.append("规则已选")
    if quant_strats:
        sources.append("策略候选")
    if theme_hit:
        sources.append("主题选股")

    # 阶段
    if "breakout" in strategies or "reversal" in strategies:
        phase = "Entry"
    elif "pullback" in strategies or "flow_follow" in strategies:
        phase = "蓄势回踩"
    else:
        phase = "观察"

    return {
        "ts_code": ts, "name": r["name"], "industry": r["industry"],
        "theme": r["industry"], "theme_heat": round(hot.get(r["industry"], 0.0), 1),
        "sources": sources, "strategies": strategies,
        "strategy_label": "、".join(STRATEGY_NAMES[s] for s in strategies),
        "phase": phase, "confidence": conf,
        "position_pct": 0.0,   # 风控后填
        "buy_low": r["buy_low"], "buy_high": r["buy_high"],
        "stop_loss": r["stop_loss"], "take_profit_1": r["take_profit_1"], "take_profit_2": r["take_profit_2"],
        "rps50": r["rps50"], "main_flow_3d": r["main_flow_3d"], "change_7d": r["change_7d"],
        "turnover": r["turnover"], "vol_ratio": r["vol_ratio"], "pct_chg": r["pct_chg"],
        "circ_mv_yi": r["circ_mv_yi"], "close": r["close"],
        # 均线结构（供前端一眼看清趋势 + 重点分）
        "above_ma20": int(bool(r["above_ma20"])), "above_ma60": int(bool(r["above_ma60"])),
        "slope_up": int(bool(r["slope_up"])),
        "is_focus": False,     # 风控后定
        "focus_score": 0.0, "star": 0,   # 重点分/星标后算
        "risk_flags": [],
    }


def _factor_score_norm(r) -> float:
    """综合因子分归一(0-1)：RPS + 主力资金 + 突破/回踩形态。"""
    s = 0.0
    s += min(r["rps50"], 100) / 100 * 0.5                       # 强弱
    s += 0.3 if r["main_flow_3d"] > 0 else 0.0                  # 资金
    s += 0.2 if (r["breakout"] or r["is_pullback"]) else 0.0    # 形态
    return min(s, 1.0)


# ── 重点分（0-100·区分度高，替代饱和的0.98置信度）+ 星标 Top5 ───────────────
# 权重：强度30 + 主力资金25 + 板块热度15 + 多路交叉15 + 量价健康10 + 均线多头5
_STAR_TOPN = 5
# 质量门槛：只留重点分≥此值的"真正强的"，收紧池子(对标吴川·宁缺毋滥)。
# 数量随行情变：弱市少(诚实)；普涨强势日再加 _POOL_MAX 封顶，避免爆量(对标吴川精选感)。
_POOL_MIN_FOCUS = 60.0
_POOL_MAX = 60


def _vol_health(vr: float) -> float:
    """量价健康度 0-1：量比 1~2.5 最佳(放量不过热)，过度放量/缩量降权。"""
    if 1.0 <= vr <= 2.5:
        return 1.0
    if 2.5 < vr <= 4.0 or 0.7 <= vr < 1.0:
        return 0.5
    return 0.2


def _ma_score(rec: dict) -> float:
    """均线结构 0-1：多头排列(站上MA20+MA60+斜率向上)=1，仅站上MA20=0.5，破位=0。"""
    if rec["above_ma20"] and rec["above_ma60"] and rec["slope_up"]:
        return 1.0
    return 0.5 if rec["above_ma20"] else 0.0


def _compute_focus_scores(records: list[dict]) -> None:
    """就地计算重点分(0-100)并星标本池最强 Top10。资金按当日横截面分位归一。"""
    if not records:
        return
    flows = sorted(r["main_flow_3d"] for r in records)
    n = len(flows)

    def flow_pct(v: float) -> float:                 # 主力资金当日排名分位 0-1
        import bisect
        return bisect.bisect_right(flows, v) / n

    for r in records:
        rps = min(max(r["rps50"], 0), 100) / 100
        cross = min(len([s for s in r["strategies"] if s != "theme_pick"]), 4) / 4
        heat = min(max(r["theme_heat"], 0), 100) / 100
        score = (rps * 30 + flow_pct(r["main_flow_3d"]) * 25 + heat * 15
                 + cross * 15 + _vol_health(r["vol_ratio"]) * 10 + _ma_score(r) * 5)
        r["focus_score"] = round(score, 1)

    for i, r in enumerate(sorted(records, key=lambda x: x["focus_score"], reverse=True)):
        r["star"] = 1 if i < _STAR_TOPN else 0


# ──────────────────────────────────────────────
# 风控
# ──────────────────────────────────────────────

def _apply_risk(records: list[dict], market_label: str, zhaban_warn: bool) -> list[dict]:
    """单主题上限 + 涨停溢价预警 + 历史胜率闸门 + 大盘仓位；定 is_focus。"""
    can_open = market_label not in ("弱势", "衰退", "数据缺失")
    pos = calc_position_pct(market_label)
    focus_conf = _FOCUS_CONF_MIN + (0.1 if zhaban_warn else 0)  # 预警时抬高门槛
    win_rates = _theme_win_rates_safe()        # {theme: {win_rate, samples, avg_return}}

    # 单主题上限：按置信度排序后每主题保留前 N 的 is_focus 资格
    per_theme: dict[str, int] = {}
    for rec in sorted(records, key=lambda x: x["confidence"], reverse=True):
        veto = _apply_winrate_gate(rec, win_rates)   # 低胜率主题 → 降级"仅观察"
        # 最关注须有真实策略信号(①~④)，仅蹭热门主题不够
        strat_hit = any(s != "theme_pick" for s in rec["strategies"])
        focus = (strat_hit and rec["confidence"] >= focus_conf and can_open and not veto)
        if focus:
            c = per_theme.get(rec["theme"], 0)
            if c >= _MAX_PER_THEME:
                focus = False
                rec["risk_flags"].append("同主题已满额")
            else:
                per_theme[rec["theme"]] = c + 1
        rec["is_focus"] = focus
        if focus:
            rec["position_pct"] = pos
        if zhaban_warn:
            rec["risk_flags"].append("昨日涨停溢价转负·需复核")
        if not can_open:
            rec["risk_flags"].append(f"大盘{market_label}·仅观察")
    return records


def _theme_win_rates_safe() -> dict[str, dict]:
    """读历史主题 T+1 胜率（现行库）；失败则返回空，不阻塞选股。"""
    try:
        from app.strategy.db import theme_win_rates
        return theme_win_rates(min_samples=_WINRATE_MIN_SAMPLES)
    except Exception as e:
        logger.warning("[选股池] 历史主题胜率读取失败，跳过闸门: %s", e)
        return {}


def _apply_winrate_gate(rec: dict, win_rates: dict[str, dict]) -> bool:
    """
    历史主题胜率前向闸门（对标吴川「PCB板T+1胜率20%→避免追涨」）。

    样本≥阈值时：胜率<35% → 降级"仅观察"并标避免追涨(返回 True=veto)；
    35%~45% → 仅警示不降级。样本不足不判（避免小样本误杀）。
    """
    wr = win_rates.get(rec["theme"])
    if not wr or wr["samples"] < _WINRATE_MIN_SAMPLES:
        return False
    pct = wr["win_rate"] * 100
    tag = f"历史T+1胜率{pct:.0f}%(样本{wr['samples']})"
    if wr["win_rate"] < _WINRATE_VETO:
        rec["risk_flags"].append(f"⛔{tag}·避免追涨")
        rec["confidence"] = round(min(rec["confidence"], 0.45), 2)   # 压低置信度
        return True
    if wr["win_rate"] < _WINRATE_WARN:
        rec["risk_flags"].append(f"⚠️{tag}·偏低")
    return False


def _zhaban_premium_warn(trade_date: str, provider: CompositeProvider) -> bool:
    """昨日涨停股今日开盘平均溢价 < -1% → 前瞻预警（你文档新增项）。"""
    try:
        from app.factors.breadth_qfq import _recent_trade_dates
        dates = _recent_trade_dates(provider, trade_date, 2)
        if len(dates) < 2:
            return False
        prev, today = dates[-2], dates[-1]
        pro = provider._ts._api
        up = pro.limit_list_d(trade_date=prev, limit_type="U")
        if up is None or up.empty:
            return False
        td = provider.get_daily(today)
        pc = provider.get_daily(prev)
        if td is None or pc is None:
            return False
        prev_close = dict(zip(pc["ts_code"], pd.to_numeric(pc["close"], errors="coerce")))
        topen = dict(zip(td["ts_code"], pd.to_numeric(td["open"], errors="coerce")))
        prems = []
        for ts in up["ts_code"]:
            if ts in prev_close and ts in topen and prev_close[ts] > 0:
                prems.append((topen[ts] - prev_close[ts]) / prev_close[ts] * 100)
        if not prems:
            return False
        avg = sum(prems) / len(prems)
        logger.info("[选股池] 昨日涨停今日溢价均值 %.2f%%", avg)
        return avg < -1.0
    except Exception as e:
        logger.debug("[选股池] 涨停溢价计算失败: %s", e)
        return False


def infer_market_label(trade_date: str, provider: CompositeProvider | None = None) -> str:
    """轻量推断大盘状态（涨停/跌停/下跌家数），用于仓位与风控。"""
    provider = provider or CompositeProvider()
    try:
        from app.strategy.market_extras import get_limit_analysis
        la = get_limit_analysis(trade_date, provider)
        up, dn = la.get("limit_up", 0), la.get("limit_down", 0)
        daily = provider.get_daily(trade_date)
        down = int((pd.to_numeric(daily["pct_chg"], errors="coerce") < 0).sum()) if daily is not None else 0
        if up >= 80 and dn < 15 and down < 2000:
            return "升温"
        if down > 3000 or dn > 30:
            return "弱势"
        return "震荡"
    except Exception as e:
        logger.debug("[选股池] 大盘状态推断失败，默认震荡: %s", e)
        return "震荡"


def generate_reasons(trade_date: str, max_n: int = 30) -> int:
    """为最关注个股生成接地式分析理由（flash, 快），写回 stock_pool.reason。"""
    from app.strategy.db import get_pool_with_perf, _conn
    from app.llm.client import LLMClient

    rows = [r for r in get_pool_with_perf(trade_date) if r.get("is_focus")][:max_n]
    if not rows:
        return 0
    llm = LLMClient()
    n = 0
    for r in rows:
        prompt = (
            f"你是严谨的A股分析师。基于以下客观因子，用1-2句话说明 {r['name']}({r['ts_code'][:6]}) 今日入选「{r['strategy_label']}」策略的逻辑。"
            f"只依据数据，不编造、不预测涨跌、不输出胜率。\n"
            f"主题{r['theme']}(热度{r['theme_heat']}) 阶段{r['phase']} RPS50={r['rps50']} "
            f"3日主力{r['main_flow_3d']}亿 7日涨幅{r['change_7d']}% 换手{r['turnover']}% 当日{r['pct_chg']}%"
        )
        try:
            reason = llm.chat([{"role": "user", "content": prompt}], task_type="flash",
                              temperature=0.3, max_tokens=200).strip()
        except Exception:
            continue
        with _conn() as con:
            con.execute("UPDATE stock_pool SET reason=? WHERE run_date=? AND ts_code=?",
                        (reason, trade_date, r["ts_code"]))
        n += 1
    logger.info("[选股池] %s 生成 %d 条理由", trade_date, n)
    return n


def _hot_themes(trade_date: str) -> dict:
    """热门行业主题 {industry: heat}（读宽表，heat≥阈值）。"""
    try:
        from app.data.theme_heat_db import get_themes
        rows = get_themes(trade_date, "industry")
        return {r["theme_name"]: (r["heat_score"] or 0) for r in rows
                if (r["heat_score"] or 0) >= _THEME_HEAT_MIN}
    except Exception:
        return {}
