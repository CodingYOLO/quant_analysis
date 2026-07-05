"""
S2：选股池策略引擎（5 内置策略 + 多路命中置信度 + 风控）。

流程：信号表(signals) → 各策略判定 → union → 多路置信度 → 风控 → 推荐池。
策略与置信度口径见《选股池设计文档》§3/§4。数字全规则计算，理由(LLM)在 S5 接入。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.factors.breadth_qfq import _recent_trade_dates
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
# 弱市板块感知开仓（A股结构化·弱市≠没机会）：板块热度≥此值+资金流入+本股多头→可试仓
_WEAK_BOARD_HEAT_MIN = 70.0
_WEAK_TRIAL_POS = 0.03       # 弱市强板块龙头的降仓试仓仓位（标准档）

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
    # 风险评分附加数据：批量取筹码获利盘 + 近10日大宗折价（出货/抛压信号）
    _load_risk_extras(records, trade_date, provider)
    # 真钱印证：近5日龙虎榜机构净买（A股仅存的个股级真机构钱）→ 供重点分加分
    _load_inst_real_money(records, trade_date, provider)
    # 因子归因补充：从已缓存因子表补 ROE/资金持续/游资接力（稳健因子入评分 + 游资标短打）
    _enrich_from_factor_table(records, trade_date)
    # 重点分(强度−风险+真钱) + 星标本池最强 Top5（始终标，供观察名单）
    _compute_focus_scores(records, market_label)
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
        # 均线结构（短线口径·含MA5/10·供前端一眼看清 + 重点分）
        "above_ma5": int(bool(r.get("above_ma5"))), "above_ma10": int(bool(r.get("above_ma10"))),
        "above_ma20": int(bool(r["above_ma20"])), "above_ma60": int(bool(r["above_ma60"])),
        "ma_bull_short": int(bool(r.get("ma_bull_short"))), "slope_up": int(bool(r["slope_up"])),
        # 风险/位置（供重点分做风险调整：过热/高位；获利盘/大宗折价后批量挂）
        "bias20": r.get("bias20", 0.0), "dist_high": r.get("dist_high", -100.0),
        "winner_rate": None, "block_discount": None,
        "is_focus": False,     # 风控后定
        "focus_score": 0.0, "star": 0, "risk_penalty": 0.0,   # 重点分/风险扣分/星标后算
        "risk_flags": [],
    }


def _factor_score_norm(r) -> float:
    """综合因子分归一(0-1)：RPS + 主力资金 + 突破/回踩形态。"""
    s = 0.0
    s += min(r["rps50"], 100) / 100 * 0.5                       # 强弱
    s += 0.3 if r["main_flow_3d"] > 0 else 0.0                  # 资金
    s += 0.2 if (r["breakout"] or r["is_pullback"]) else 0.0    # 形态
    return min(s, 1.0)


# ── 重点分（0-100·风险调整）+ 星标 Top5 ──────────────────────────────────────
# 强度分(0-100·因子归因校准)：RPS20 + 主力资金30 + 板块热度12 + 多路交叉12 + 量价8 + 资金持续8 + ROE质量5 + 均线5
# 风险扣分(0-_RISK_MAX)：乖离过热 + 近期追高 + 逼近历史高位 → 只罚极端过热(主升强者恒强保留)
# 重点分 = 强度分 − 风险扣分。避免把"加速赶顶"的高位票排第一。
_STAR_TOPN = 5
# 风险扣分坡度 (起罚, 封顶, 满分扣分)：温和强势不罚、极端过热重罚
_RISK_BIAS = (8.0, 28.0, 12.0)    # 20日乖离率：8%起、28%封顶 → 0~12
_RISK_CHASE = (15.0, 38.0, 8.0)   # 7日涨幅：15%起、38%封顶 → 0~8（追高）
_RISK_HIGH = (-6.0, 0.0, 6.0)     # 距120日高：跌破6%内开始、新高封顶 → 0~6（高位）
_RISK_WINNER = (85.0, 96.0, 6.0)  # 获利盘：85%起、96%封顶 → 0~6（普遍获利=抛压）
# 获利盘抛压的"位置门控"：仅高位才算抛压（距120日高 -15%→-3% 把扣分从0放大到满）。
# 低位/中位的高获利盘可能是主力吸筹/突破，不该当风险——保护"筹码集中=拉升前"那类票。
_WINNER_POS_GATE = (-15.0, -3.0)
_RISK_BLOCK = (2.0, 9.0, 5.0)     # 大宗折价幅度：2%起、9%封顶 → 0~5（折价出货·直接卖压不门控）
# 当日破位大跌：跌破MA5/10 且当日下跌 → 按跌幅扣分（修"前期强但今天破位还排第一"的后视镜缺陷）。
_RISK_BREAKDOWN = (2.0, 9.0, 14.0)   # 当日跌幅(跌破MA5/10时)：2%起、9%封顶 → 0~14；放量再×1.3(资金在撤·别接刀)
_RISK_MAX = 36.0                   # 风险扣分上限
# 龙虎榜机构「真钱」加分：把 A股仅存的个股级真机构钱(机构席位净买)纳入重点分，让"真钱看好"的票排前。
# 上榜稀疏→大多数票无足迹(=0分·不罚)；净买加分、净卖扣分；弱市真钱稀缺→加倍看重。
_INST_LOOKBACK = 5                  # 机构净买回看天数(与资金三角一致)
_INST_EPS_YI = 0.05                # 机构净买/卖"近似零"带宽(亿)
_W_INST_BUY = (0.1, 3.0, 12.0)     # 机构净买(亿)：0.1起、3亿封顶 → 0~12 基础加分
_W_INST_DAY = 2.0                  # 每多一天机构净买 +2(真钱持续吸筹·置信更高)
_INST_BUY_CAP = 18.0               # 真钱加分上限
_W_INST_SELL = (0.1, 3.0, 20.0)    # 机构净卖(亿)：0~20 扣分(真钱在撤·更重)
_WEAK_INST_MULT = 1.5              # 弱市真钱加分×1.5(稀缺·更该看重)
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
    """
    均线结构 0-1（短线口径·含 MA5/MA10）：
      完整多头排列(MA5>10>20>60 + 趋势向上)=1 ＞ 短期多头(MA5>10>20)=0.85
      ＞ 站上MA5/MA10=0.65 ＞ 仅站上MA20(短期交织)=0.4 ＞ 跌破=0.15/破位=0。
    """
    a5, a10 = rec.get("above_ma5", 0), rec.get("above_ma10", 0)
    a20, a60 = rec["above_ma20"], rec["above_ma60"]
    bull_s = rec.get("ma_bull_short", 0)        # MA5>MA10>MA20
    if a5 and a10 and a20 and a60 and bull_s and rec["slope_up"]:
        return 1.0
    if a5 and a10 and a20 and bull_s:
        return 0.85
    if a5 and a10:
        return 0.65
    if a20:
        return 0.4
    return 0.15 if a60 else 0.0


def _ramp(v: float, lo: float, hi: float) -> float:
    """v 从 lo→hi 线性映射到 0→1（越界裁剪）。"""
    if v <= lo:
        return 0.0
    if v >= hi:
        return 1.0
    return (v - lo) / (hi - lo)


def _risk_penalty(rec: dict) -> float:
    """
    风险扣分（0~_RISK_MAX）：过热(乖离) + 追高(7日) + 高位(距高) + 抛压(获利盘) + 出货(大宗折价)。
    只罚极端（坡度起点之内不罚），尊重主升浪"强者恒强"，不一刀切毙高位龙头。
    """
    lo, hi, w = _RISK_BIAS;  p_bias = _ramp(rec.get("bias20", 0.0), lo, hi) * w
    lo, hi, w = _RISK_CHASE; p_chase = _ramp(rec.get("change_7d", 0.0), lo, hi) * w
    dist_h = rec.get("dist_high", -100.0)
    lo, hi, w = _RISK_HIGH;  p_high = _ramp(dist_h, lo, hi) * w
    # 获利盘抛压：×位置门控 → 仅高位才算（低位高获利盘=吸筹/突破，不罚）
    lo, hi, w = _RISK_WINNER; p_win = _ramp(rec.get("winner_rate") or 0.0, lo, hi) * w
    p_win *= _ramp(dist_h, *_WINNER_POS_GATE)
    # 大宗折价：block_discount 负=折价；取折价幅度(正值)罚（直接卖压，不门控）
    disc = -(rec.get("block_discount") or 0.0)
    lo, hi, w = _RISK_BLOCK; p_blk = _ramp(disc, lo, hi) * w
    # 当日破位大跌：仅当已跌破MA5/10(短线走坏)且当日下跌才罚；放量破位=资金撤·加重(别接刀)
    p_break = 0.0
    broke_short = not (rec.get("above_ma5") and rec.get("above_ma10"))
    drop = -(rec.get("pct_chg") or 0.0)
    if broke_short and drop > 0:
        lo, hi, w = _RISK_BREAKDOWN; p_break = _ramp(drop, lo, hi) * w
        if (rec.get("vol_ratio") or 1.0) >= 1.5:
            p_break *= 1.3
    return round(min(p_bias + p_chase + p_high + p_win + p_blk + p_break, _RISK_MAX), 1)


def _inst_bonus(rec: dict, market_label: str) -> float:
    """龙虎榜机构『真钱』加分：净买加分(含持续天数·弱市×1.5)、净卖扣分。无足迹/近零=0。

    真钱>估算：机构席位是 A股个股层面唯一公开的真金白银，比"主力估算"硬得多。
    """
    inst = rec.get("inst_net_yi")
    if inst is None:
        return 0.0
    if inst >= _INST_EPS_YI:                              # 机构真买 → 加分
        lo, hi, w = _W_INST_BUY
        b = min(_ramp(inst, lo, hi) * w + (rec.get("inst_buy_days") or 0) * _W_INST_DAY, _INST_BUY_CAP)
        if market_label in ("弱势", "衰退"):
            b *= _WEAK_INST_MULT
        return round(b, 1)
    if inst <= -_INST_EPS_YI:                             # 机构净卖 → 扣分(真钱在撤)
        lo, hi, w = _W_INST_SELL
        return -round(_ramp(-inst, lo, hi) * w, 1)
    return 0.0


def _load_inst_real_money(records: list[dict], trade_date: str, provider: CompositeProvider) -> None:
    """批量取近5日龙虎榜机构净买(真钱)挂到 records（供重点分真钱加分）。上榜稀疏·失败不阻塞。"""
    inst_map: dict = {}
    try:
        from app.strategy.fund_triangle import _inst_net_map
        dates = _recent_trade_dates(provider, trade_date, _INST_LOOKBACK)
        inst_map = _inst_net_map(provider, dates)         # {ts: (机构净买亿, 净买天数)}
    except Exception as e:
        logger.debug("[选股池] 龙虎榜机构净买获取失败（忽略）: %s", e)
    for r in records:
        v = inst_map.get(r["ts_code"])
        r["inst_net_yi"] = round(float(v[0]), 2) if v else None
        r["inst_buy_days"] = int(v[1]) if v else 0


def _load_risk_extras(records: list[dict], trade_date: str, provider: CompositeProvider) -> None:
    """批量取筹码获利盘 + 近10日大宗折价，挂到 records（供风险扣分）。全市场批量·失败不阻塞。"""
    winner: dict[str, float] = {}
    try:
        cyq = provider.get_cyq_perf_by_date(trade_date)
        if cyq is not None and not cyq.empty and "winner_rate" in cyq.columns:
            winner = dict(zip(cyq["ts_code"].astype(str),
                              pd.to_numeric(cyq["winner_rate"], errors="coerce")))
    except Exception as e:
        logger.debug("[选股池] 筹码批量获取失败（忽略）: %s", e)
    block = _recent_block_discount(provider, trade_date)
    for r in records:
        ts = r["ts_code"]
        wr = winner.get(ts)
        r["winner_rate"] = round(float(wr), 1) if wr is not None and not pd.isna(wr) else None
        b = block.get(ts)
        r["block_discount"] = b if b is not None else None      # 量加权平均折溢价(%)，负=折价


def _recent_block_discount(provider: CompositeProvider, end_date: str, n: int = 10) -> dict[str, float]:
    """近 n 个交易日大宗交易的量加权平均折溢价（%，负=折价/出货）。{ts_code: prem}。"""
    agg: dict[str, list[float]] = {}    # ts -> [Σ(prem×amount), Σamount]
    try:
        dates = _recent_trade_dates(provider, end_date, n)
    except Exception:
        return {}
    for d in dates:
        try:
            bt = provider.get_block_trade_by_date(d)
            daily = provider.get_daily(d)
        except Exception:
            continue
        if bt is None or bt.empty or daily is None or daily.empty:
            continue
        close = dict(zip(daily["ts_code"].astype(str), pd.to_numeric(daily["close"], errors="coerce")))
        for ts, price, amt in zip(bt["ts_code"].astype(str),
                                  pd.to_numeric(bt["price"], errors="coerce"),
                                  pd.to_numeric(bt["amount"], errors="coerce")):
            c = close.get(ts)
            if not c or c <= 0 or pd.isna(price) or price <= 0 or pd.isna(amt) or amt <= 0:
                continue
            prem = (price / c - 1) * 100
            s = agg.setdefault(ts, [0.0, 0.0])
            s[0] += prem * amt
            s[1] += amt
    return {ts: round(s[0] / s[1], 2) for ts, s in agg.items() if s[1] > 0}


def _enrich_from_factor_table(records: list[dict], trade_date: str) -> None:
    """best-effort：从**已缓存**因子表补 roe/consec_inflow/youzi_relay_days（不触发构建·无缓存跳过）。

    因子归因(近1年T+10 IC)显示：ROE/资金持续=🟢稳健正alpha·游资接力=🔴稳健负(波段避雷)。
    日更流水线/暖机当日已建因子表→通常命中缓存；缺则各字段留默认(评分优雅降级)。
    """
    try:
        import pandas as pd
        from app.strategy.screener import _factor_cache_path
        p = _factor_cache_path(trade_date)
        if not p.exists():
            logger.debug("[选股池] 因子表未缓存 %s·跳过ROE/资金持续补充", trade_date)
            return
        ft = pd.read_parquet(p)
        cols = [c for c in ("roe", "consec_inflow", "youzi_relay_days") if c in ft.columns]
        m = ft.set_index("ts_code")[cols]
        for r in records:
            if r["ts_code"] in m.index:
                row = m.loc[r["ts_code"]]
                if "roe" in cols and pd.notna(row["roe"]):
                    r["roe"] = float(row["roe"])
                if "consec_inflow" in cols and pd.notna(row["consec_inflow"]):
                    r["consec_inflow"] = int(row["consec_inflow"])
                if "youzi_relay_days" in cols and pd.notna(row["youzi_relay_days"]):
                    r["youzi_relay_days"] = int(row["youzi_relay_days"])
    except Exception as e:
        logger.debug("[选股池] 因子表补充失败(跳过·不影响): %s", e)


def _compute_focus_scores(records: list[dict], market_label: str = "震荡") -> None:
    """就地计算重点分(强度分 − 风险扣分 + 龙虎榜机构真钱加分)并星标本池最强 Top5。

    权重（因子归因校准·2026-07）：把过重的 RPS(🟡弱/看regime) 30→20，让给稳健正alpha——
    主力资金 25→30、新增 资金持续8 + ROE质量5；游资接力票标🎲短打(🔴波段避雷)。
    """
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
        persist = min(max(r.get("consec_inflow", 0), 0), 5) / 5           # 资金持续(🟢稳健)
        roe_q = min(max(r.get("roe", 0.0) or 0.0, 0.0), 8.0) / 8.0        # ROE质量(🟢稳健)
        strength = (rps * 20 + flow_pct(r["main_flow_3d"]) * 30 + heat * 12
                    + cross * 12 + _vol_health(r["vol_ratio"]) * 8
                    + persist * 8 + roe_q * 5 + _ma_score(r) * 5)
        if r.get("youzi_relay_days", 0) >= 3:                             # 🎲游资接力=T+1打板属性·非波段
            fl = r.setdefault("risk_flags", [])
            if not any("短打" in str(x) for x in fl):
                fl.append("🎲短打·游资接力(T+1打板属性·非波段·归因显示波段跑输)")
        r["risk_penalty"] = _risk_penalty(r)
        r["inst_bonus"] = _inst_bonus(r, market_label)                        # 龙虎榜机构真钱加分
        r["focus_score"] = round(min(max(strength - r["risk_penalty"] + r["inst_bonus"], 0.0), 100.0), 1)

    for i, r in enumerate(sorted(records, key=lambda x: x["focus_score"], reverse=True)):
        r["star"] = 1 if i < _STAR_TOPN else 0


# ──────────────────────────────────────────────
# 风控
# ──────────────────────────────────────────────

def _open_gate(rec: dict, market_label: str) -> tuple[bool, float]:
    """
    开仓闸门（板块感知·A股结构化）：返回 (是否可开, 建议仓位)。

    - 强势/震荡市：正常开仓，仓位按大盘。
    - **弱市不一刀切**：板块强（热度≥阈值 + 主力资金流入 + 本股多头排列）的主线龙头
      仍可做，但**降仓试仓**（弱市β逆风，控风险）；板块也弱→观察。
    - 数据缺失：一律不开（无可靠依据）。
    """
    if market_label == "数据缺失":
        return False, 0.0
    if market_label not in ("弱势", "衰退"):
        return True, calc_position_pct(market_label)
    board_strong = (rec["theme_heat"] >= _WEAK_BOARD_HEAT_MIN
                    and rec["main_flow_3d"] > 0 and rec["above_ma20"])
    if board_strong:
        rec["risk_flags"].append("弱市·强板块龙头·降仓试仓")
        return True, _WEAK_TRIAL_POS
    rec["risk_flags"].append(f"大盘{market_label}·板块未走强·仅观察")
    return False, 0.0


def _apply_risk(records: list[dict], market_label: str, zhaban_warn: bool) -> list[dict]:
    """单主题上限 + 涨停溢价预警 + 历史胜率闸门 + 板块感知开仓闸门；定 is_focus。"""
    focus_conf = _FOCUS_CONF_MIN + (0.1 if zhaban_warn else 0)  # 预警时抬高门槛
    win_rates = _theme_win_rates_safe()        # {theme: {win_rate, samples, avg_return}}

    # 单主题上限：按置信度排序后每主题保留前 N 的 is_focus 资格
    per_theme: dict[str, int] = {}
    for rec in sorted(records, key=lambda x: x["confidence"], reverse=True):
        veto = _apply_winrate_gate(rec, win_rates)   # 低胜率主题 → 降级"仅观察"
        # 最关注须有真实策略信号(①~④)，仅蹭热门主题不够
        strat_hit = any(s != "theme_pick" for s in rec["strategies"])
        can_open, pos = _open_gate(rec, market_label)   # 板块感知：弱市强板块仍可试仓
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
            f"你是资深A股分析师。基于以下客观因子，用1-2句话说清 {r['name']}({r['ts_code'][:6]}) 今日入选「{r['strategy_label']}」的逻辑，"
            f"并点出它的**亮点和要注意的点**(强在哪、有无过热/追高风险)——别只复述数字。"
            f"只依据数据、不编造、不编胜率%、不打包票必涨。\n"
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
