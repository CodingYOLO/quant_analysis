"""
节点 C: 选股线（吴川体系第三层，核心可回测模块）。

五步过滤流水线（每步都可独立调参/回测）：
  Step 1: 基础门槛过滤  → 市值、排除ST、排除科创板（可选）
  Step 2: 趋势与均线    → 站上MA20、MA60，MA均线向上
  Step 3: 量价结构      → 换手率适中、MACD金叉或缩量回踩MA20
  Step 4: 资金面        → 超大单+大单净流入连续3日为正
  Step 5: 相对强弱      → RPS50≥90（近50日跑赢90%个股，回测验证门槛）

候选股按综合评分降序排列，取前 MAX_CANDIDATES 只。
所有过滤条件均可通过 .env 配置，不硬编码。
"""

import logging

import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix, get_stock_history
from app.factors import (
    ma, macd_golden_cross, calc_rps,
    volume_ratio, pullback_quality_score, above_ma, ma_slope,
    calc_vwap, calc_stop_loss_price, calc_take_profit_prices,
    calc_buy_zones, calc_position_pct, rsi, vwap_position,
)
from app.state import Candidate, PipelineState, StockFactors, TradePlan

logger = logging.getLogger(__name__)


def node_stock_selection(state: PipelineState) -> PipelineState:
    """
    融合版选股（路线B）：候选统一来自「选股池」引擎
    （已纳入板块热度/多周期资金/人气/Top100拥挤度/次日风险/多路交叉验证）。
    叠加：① 当天数据新鲜度校验（必须等数据就绪）② 个股避雷否决。
    老五步评分(_run_selection_pipeline)已退役（保留代码不调用）。
    """
    provider = CompositeProvider()
    settings = get_settings()
    td = state.trade_date
    regime = state.market_regime
    market_label = regime.label if regime else "震荡"
    can_open = regime.can_open if regime else True

    # S1: 数据新鲜度校验——绝不用半截数据
    ok, msg = _data_ready(td, provider)
    if not ok:
        logger.warning("[节点C] 当天数据未就绪，跳过选股: %s", msg)
        if state.meta:
            state.meta.errors.append(f"选股跳过（数据未就绪）：{msg}")
        state.candidates = []
        return state
    _ensure_wide(td, provider)   # 主题热度/板块全景宽表就绪（theme_pick 依赖），缺则现算

    # 统一选股逻辑：调用选股池引擎
    from app.strategy.stock_pool import build_stock_pool
    pool = build_stock_pool(td, provider, market_label=market_label, persist=True)
    if not pool:
        state.candidates = []
        logger.info("[节点C] 选股池为空")
        return state

    # 可开仓→取最关注；不可开仓(弱势)→全池按置信度作观察候选
    picks = [r for r in pool if r["is_focus"]] if can_open else pool
    picks = sorted(picks, key=lambda r: r["confidence"], reverse=True)[:settings.max_candidates]

    # S2: 个股避雷（结构化减持/立案 + 博查最新负面）→ 降级"仅观察"
    guard = _news_guard_map(td, provider, picks)
    # 富化补算 rsi/vwap/振幅（仅候选小集合）
    extra = _enrich_factors(td, provider, [r["ts_code"] for r in picks])

    state.candidates = [
        _pool_to_candidate(r, extra.get(r["ts_code"], {}),
                           guard.get(r["ts_code"][:6], {}), market_label, can_open)
        for r in picks
    ]
    n_red = sum(1 for v in guard.values() if v.get("red"))
    logger.info("[节点C] 融合选股完成：候选 %d 只（can_open=%s，重大利空降级 %d）",
                len(state.candidates), can_open, n_red)
    return state


# ============================================================
# 融合版助手（S1 数据校验 / S2 避雷 / 候选映射）
# ============================================================

def _data_ready(td: str, provider: CompositeProvider) -> tuple[bool, str]:
    """校验当天核心数据是否就绪（日线/资金流/每日指标）。"""
    try:
        daily = provider.get_daily(td)
        if daily is None or daily.empty:
            return False, f"{td} 日线未入库"
        mf = provider.get_money_flow(td)
        if mf is None or mf.empty:
            return False, f"{td} 个股资金流未入库（约17:15）"
        db = provider.get_daily_basic(td)
        if db is None or db.empty:
            return False, f"{td} 每日指标(换手/市值)未入库"
        return True, "ok"
    except Exception as e:
        return False, f"数据校验异常: {e}"


def _ensure_wide(td: str, provider: CompositeProvider) -> None:
    """确保主题宽表已计算（theme_pick/板块热度依赖），缺则现算。"""
    try:
        from app.data.theme_heat_db import get_themes
        if get_themes(td, "industry"):
            return
        from app.factors.theme_wide import build_industry_wide, build_concept_wide
        logger.info("[节点C] %s 宽表缺失，现算行业+概念...", td)
        build_industry_wide(td, provider, persist=True)
        build_concept_wide(td, provider, persist=True)
    except Exception as e:
        logger.warning("[节点C] 宽表确保失败（板块热度可能缺）: %s", e)


# 重大利空(降级)关键词 vs 一般风险(仅警示)关键词——避免误报否决好票
_RED_KW = {"立案", "调查", "退市", "*ST", "ST", "诉讼", "仲裁", "处罚", "违规",
           "违法", "造假", "冻结", "爆仓", "暴雷", "踩雷"}
_RED_FORECAST = {"预亏", "预减", "首亏", "续亏"}


def _news_guard_map(td: str, provider: CompositeProvider, picks: list[dict]) -> dict[str, dict]:
    """
    分级避雷：返回 {6位码: {"red":[重大利空,降级], "yellow":[一般风险,仅警示]}}。
    红线(可信)：业绩预亏/预减 + 立案/退市/诉讼/处罚/造假/冻结。
    黄线(常见,不降级)：减持/问询/核查/监管/质押 等。
    """
    out: dict[str, dict] = {}

    def add(code6, tier, tag):
        out.setdefault(code6, {"red": [], "yellow": []})[tier].append(tag)

    # 结构化（Tushare，可信）
    try:
        from app.strategy.market_extras import get_forecast_risk, get_holder_reduce
        forecast = get_forecast_risk(td, provider)
        reduce = get_holder_reduce(td, provider)
        for r in picks:
            ts = r["ts_code"]
            if ts in forecast:
                tier = "red" if forecast[ts] in _RED_FORECAST else "yellow"
                add(ts[:6], tier, f"业绩{forecast[ts]}")
            if ts in reduce:
                add(ts[:6], "yellow", reduce[ts])    # 减持=黄线，不降级
    except Exception as e:
        logger.debug("[节点C] 结构化避雷失败: %s", e)

    # 博查最新负面（按命中关键词分红/黄线）
    try:
        from types import SimpleNamespace
        from app.strategy.news_guard import scan_candidates
        cands = [SimpleNamespace(name=r["name"], code=r["ts_code"]) for r in picks]
        for code6, hs in scan_candidates(cands).items():
            red = sorted({h["keyword"] for h in hs if h["keyword"] in _RED_KW})
            yellow = sorted({h["keyword"] for h in hs if h["keyword"] not in _RED_KW})
            if red:
                add(code6, "red", "网络舆情:" + "/".join(red[:3]))
            if yellow:
                add(code6, "yellow", "网络舆情:" + "/".join(yellow[:3]))
    except Exception as e:
        logger.debug("[节点C] 博查避雷失败: %s", e)
    return out


def _enrich_factors(td: str, provider: CompositeProvider, codes: list[str]) -> dict[str, dict]:
    """为候选补算 rsi_14 / vwap_deviation / avg_amplitude_5d（小集合，复用价格矩阵）。"""
    out: dict[str, dict] = {}
    if not codes:
        return out
    try:
        close_m, _o, high_m, low_m, vol_m = load_price_matrix(td, provider, n_days=30)
    except Exception:
        return out
    for ts in codes:
        if ts not in close_m.columns:
            continue
        try:
            close = close_m[ts].dropna()
            vol = vol_m[ts].dropna() if ts in vol_m.columns else pd.Series(dtype=float)
            rsi_v = float(rsi(close, 14).iloc[-1]) if len(close) >= 15 else 0.0
            vwap_dev = float(vwap_position(close, vol, 20)) if (len(close) >= 20 and len(vol) >= 20) else 0.0
            amp = 0.0
            if ts in high_m.columns and ts in low_m.columns:
                amp_series = (high_m[ts] - low_m[ts]) / close_m[ts].shift(1) * 100
                amp = float(amp_series.dropna().tail(5).mean()) if amp_series.dropna().tail(5).size else 0.0
            out[ts] = {"rsi_14": round(rsi_v, 1), "vwap_deviation": round(vwap_dev, 1),
                       "avg_amplitude_5d": round(amp, 1)}
        except Exception:
            continue
    return out


def _pool_to_candidate(r: dict, extra: dict, guard: dict,
                       market_label: str, can_open: bool) -> Candidate:
    """选股池记录 → Candidate（映射因子/交易计划；红线避雷降级仅观察，黄线仅警示）。"""
    red = guard.get("red", []) if guard else []
    yellow = guard.get("yellow", []) if guard else []
    observe = (not can_open) or bool(red)
    f = StockFactors(
        close=r.get("close", 0.0), pct_change=r.get("pct_chg", 0.0),
        turnover_rate=r.get("turnover", 0.0), market_cap=r.get("circ_mv_yi", 0.0),
        fund_flow_3d=r.get("main_flow_3d", 0.0) * 1e4,   # 亿→万元
        rps50=r.get("rps50", 0.0), change_pct_7d=r.get("change_7d", 0.0),
        rsi_14=extra.get("rsi_14", 0.0), vwap_deviation=extra.get("vwap_deviation", 0.0),
        avg_amplitude_5d=extra.get("avg_amplitude_5d", 0.0),
    )
    p = TradePlan(
        buy_conservative=r.get("buy_low", 0.0), buy_aggressive=r.get("buy_high", 0.0),
        stop_loss=r.get("stop_loss", 0.0), take_profit_1=r.get("take_profit_1", 0.0),
        take_profit_2=r.get("take_profit_2", 0.0),
        position_pct=0.0 if observe else r.get("position_pct", 0.0),
    )
    filters = [x for x in (list(r.get("sources", [])) + [r.get("strategy_label", "")]) if x]
    risk = list(r.get("risk_flags", []))
    risk += [f"🔴{t}" for t in red] + [f"🟡{t}" for t in yellow]
    if red:
        risk.append("命中重大利空·降级仅观察")
    return Candidate(
        code=r["ts_code"], name=r["name"], theme=r.get("theme", ""),
        filters_passed=filters, risk_flags=risk, trade_plan=p, factors=f,
    )


# ============================================================
# 主流水线
# ============================================================

def _extract_event_industries(state: PipelineState) -> set[str]:
    """
    从 state.themes 中提取有事件催化（phase='事件驱动↑'）的行业集合。
    用于在选股评分中给热门事件驱动板块加分（+20分）。
    """
    industries: set[str] = set()
    for theme in state.themes:
        if theme.phase in ("事件驱动↑", "升温") and theme.heat >= 6:
            industries.update(theme.concept_codes)  # concept_codes 存储的是行业名列表
    if industries:
        logger.info("[节点C] 事件催化行业: %s", ", ".join(sorted(industries)))
    return industries


def _run_selection_pipeline(
    trade_date: str,
    provider: CompositeProvider,
    max_candidates: int,
    min_market_cap: float,
    max_market_cap: float,
    market_label: str = "震荡",
    event_catalyst_industries: set[str] | None = None,
) -> list[Candidate]:
    """五步过滤，返回候选股列表。"""

    # --- 加载基础数据 ---
    logger.info("加载 %s 日线基础数据...", trade_date)
    daily = provider.get_daily(trade_date)
    daily_basic = provider.get_daily_basic(trade_date)
    money_flow = provider.get_money_flow(trade_date)
    stock_basic = provider.get_stock_basic()
    lhb = provider.get_lhb_detail(trade_date)
    north_flow = provider.get_north_flow(trade_date)

    # 千股千评（人气排名+庄家控盘）— VPN问题解决后自动生效，失败时静默跳过
    stock_comment = _safe_get_stock_comment(trade_date, provider)

    # 加载历史价格矩阵（用于计算均线/MACD/RPS）
    logger.info("加载历史行情矩阵（近65日）...")
    close_m, open_m, high_m, low_m, vol_m = load_price_matrix(trade_date, provider, n_days=65)

    # 计算全市场 RPS
    rps50 = calc_rps(close_m, n=50)
    rps120 = calc_rps(close_m, n=120)

    # 构建合并表（以 daily 为基础）
    universe = _build_universe(daily, daily_basic, stock_basic, money_flow, stock_comment)
    logger.info("全市场股票数: %d", len(universe))

    # Step 1: 基础门槛
    universe = _filter_basic(universe, min_market_cap, max_market_cap)
    logger.info("Step1 基础门槛后: %d 只", len(universe))

    # Step 2~4 需要逐只计算技术因子（只对通过Step1的股票计算，节省时间）
    candidates_raw = _apply_technical_filters(
        universe=universe,
        close_m=close_m,
        open_m=open_m,
        high_m=high_m,
        low_m=low_m,
        vol_m=vol_m,
        rps50=rps50,
        rps120=rps120,
        money_flow=money_flow,
        lhb_codes=set(lhb["ts_code"].tolist()) if lhb is not None and not lhb.empty else set(),
        north_net=_get_north_net_by_stock(north_flow),
        event_catalyst_industries=event_catalyst_industries or set(),
    )

    logger.info("技术因子过滤后: %d 只", len(candidates_raw))

    # Step 5: 按综合评分排序，取 top N
    if candidates_raw.empty or "total_score" not in candidates_raw.columns:
        logger.warning("技术因子过滤后无候选股，返回空列表")
        return []
    candidates_raw = candidates_raw.sort_values("total_score", ascending=False)
    top = candidates_raw.head(max_candidates)

    return _build_candidate_objects(
        top, stock_basic,
        market_label=market_label,
        event_catalyst_industries=event_catalyst_industries or set(),
    )


# ============================================================
# 数据合并
# ============================================================

def _safe_get_stock_comment(trade_date: str, provider: CompositeProvider) -> pd.DataFrame | None:
    """安全获取千股千评，失败时返回 None（不影响主流程）。"""
    try:
        df = provider.get_stock_comment(trade_date)
        if df is not None and not df.empty:
            # 字段：代码, 综合得分, 目前排名, 机构参与度, 主力成本
            df = df.rename(columns={
                "代码": "symbol",
                "综合得分": "comment_score",
                "目前排名": "popularity_rank",
                "机构参与度": "institution_pct",
                "主力成本": "main_cost",
            })
            return df
    except Exception as e:
        logger.debug("千股千评获取失败（VPN问题或接口变更），跳过: %s", e)
    return None


def _build_universe(
    daily: pd.DataFrame,
    daily_basic: pd.DataFrame,
    stock_basic: pd.DataFrame,
    money_flow: pd.DataFrame,
    stock_comment: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """合并当日行情、基础指标、资金流，构建全市场候选表。"""
    # daily_basic: circ_mv/total_mv 单位是万元
    basic_cols = ["ts_code", "circ_mv", "total_mv", "turnover_rate", "volume_ratio", "pe_ttm", "pb"]
    db = daily_basic[basic_cols].copy() if daily_basic is not None and not daily_basic.empty else pd.DataFrame()

    # 资金流：计算超大单+大单净流入
    if money_flow is not None and not money_flow.empty:
        mf = money_flow[["ts_code", "buy_elg_amount", "sell_elg_amount", "buy_lg_amount", "sell_lg_amount", "net_mf_amount"]].copy()
        mf["main_net_amount"] = (
            (mf["buy_elg_amount"] - mf["sell_elg_amount"]) +
            (mf["buy_lg_amount"] - mf["sell_lg_amount"])
        )
    else:
        mf = pd.DataFrame(columns=["ts_code", "main_net_amount", "net_mf_amount"])

    # 合并
    uni = daily[["ts_code", "open", "high", "low", "close", "pct_chg", "vol", "amount"]].copy()
    if not db.empty:
        uni = uni.merge(db, on="ts_code", how="left")
    if not mf.empty:
        uni = uni.merge(mf[["ts_code", "main_net_amount", "net_mf_amount"]], on="ts_code", how="left")

    # 股票名称
    if stock_basic is not None and not stock_basic.empty:
        uni = uni.merge(stock_basic[["ts_code", "name", "industry"]], on="ts_code", how="left")

    # 千股千评（人气排名、综合得分）— 用6位 symbol 关联
    if stock_comment is not None and not stock_comment.empty:
        uni["symbol"] = uni["ts_code"].str[:6]
        comment_cols = [c for c in ["symbol", "comment_score", "popularity_rank", "main_cost"] if c in stock_comment.columns]
        uni = uni.merge(stock_comment[comment_cols], on="symbol", how="left")
        uni = uni.drop(columns=["symbol"], errors="ignore")

    uni["circ_mv_100m"] = uni.get("circ_mv", pd.Series(0.0, index=uni.index)).fillna(0) / 10000  # 万元→亿元
    return uni


# ============================================================
# Step 1: 基础门槛过滤
# ============================================================

def _filter_basic(universe: pd.DataFrame, min_cap: float, max_cap: float) -> pd.DataFrame:
    """
    过滤条件：
    - 市值在 [min_cap, max_cap] 亿元之间
    - 排除 ST/*ST 股票
    - 排除当日停牌（成交量为0）
    - 排除当日跌停（无法买入）
    - 成交额≥1亿（流动性保证）
    """
    df = universe.copy()

    # 市值过滤
    if "circ_mv_100m" in df.columns:
        df = df[(df["circ_mv_100m"] >= min_cap) & (df["circ_mv_100m"] <= max_cap)]

    # 排除ST
    if "name" in df.columns:
        df = df[~df["name"].str.contains("ST|退", na=False)]

    # 排除停牌（成交量为0）
    df = df[df["vol"] > 0]

    # 排除跌停（无法买入，T+1制度下跌停板买不进）
    df = df[df["pct_chg"] > -9.4]

    # 成交额≥1亿（amount 单位：千元）
    df = df[df["amount"] >= 10000]

    return df.reset_index(drop=True)


# ============================================================
# Step 2~4: 技术因子计算与过滤
# ============================================================

def _apply_technical_filters(
    universe: pd.DataFrame,
    close_m: pd.DataFrame,
    open_m: pd.DataFrame,
    high_m: pd.DataFrame,
    low_m: pd.DataFrame,
    vol_m: pd.DataFrame,
    rps50: pd.Series,
    rps120: pd.Series,
    money_flow: pd.DataFrame,
    lhb_codes: set,
    north_net: dict,
    event_catalyst_industries: set[str] | None = None,
) -> pd.DataFrame:
    """
    对每只股票计算技术因子，筛选并评分。
    """
    results = []

    for _, row in universe.iterrows():
        ts_code = row["ts_code"]
        hist = get_stock_history(ts_code, close_m, open_m, high_m, low_m, vol_m)
        close = hist["close"]
        vol = hist["vol"]
        open_ = hist["open"]
        low = hist["low"]

        if len(close) < 25:
            continue

        # ---- Step 2: 趋势与均线 ----
        filters_passed = []
        filters_failed = []

        if above_ma(close, 20):
            filters_passed.append("站上MA20")
        else:
            filters_failed.append("未站上MA20")
            # MA20 是硬过滤条件
            continue

        above_ma60 = above_ma(close, 60) if len(close) >= 62 else False
        if above_ma60:
            filters_passed.append("站上MA60")

        ma20_rising = ma_slope(close, 20) > 0
        if ma20_rising:
            filters_passed.append("MA20向上")
        else:
            filters_failed.append("MA20下行")

        # ---- Step 3: 量价结构 ----
        vr = volume_ratio(vol)
        turnover = row.get("turnover_rate", 0) or 0

        # 换手率过滤：1%~15%（太低无人气，太高散户情绪透支）
        if not (1.0 <= turnover <= 15.0):
            filters_failed.append(f"换手率{turnover:.1f}%不在1%-15%")
            continue
        filters_passed.append(f"换手率{turnover:.1f}%")

        # 振幅过滤：近5日平均振幅≥3%，短线票必须有足够波动空间
        # 振幅 = (high - low) / 前收 * 100
        high_s = hist["high"]
        low_s = hist["low"]
        if len(high_s) >= 5 and len(close) >= 6:
            pre_close = close.shift(1)
            amplitude = ((high_s - low_s) / pre_close * 100).dropna()
            avg_amplitude = float(amplitude.iloc[-5:].mean())
        else:
            avg_amplitude = 0.0

        if avg_amplitude < 3.0:
            filters_failed.append(f"近5日均振幅{avg_amplitude:.1f}%<3%，波动不足")
            continue
        filters_passed.append(f"均振幅{avg_amplitude:.1f}%")

        # 核心形态判断：MACD金叉 或 缩量回踩MA20
        is_macd_cross = macd_golden_cross(close) if len(close) >= 35 else False
        pullback_score = pullback_quality_score(close, vol, open_, low)
        is_pullback = pullback_score >= 50

        if is_macd_cross:
            filters_passed.append("MACD金叉")
        if is_pullback:
            filters_passed.append(f"缩量回踩(得分{pullback_score:.0f})")

        # 至少满足一个形态信号
        if not is_macd_cross and not is_pullback:
            filters_failed.append("无有效形态信号")
            continue

        # ---- Step 4: 主力资金 ----
        main_net = row.get("main_net_amount", 0) or 0
        if main_net > 0:
            filters_passed.append(f"主力净流入{main_net/10000:.1f}亿")
        else:
            filters_failed.append(f"主力净流出{main_net/10000:.1f}亿")
            # 资金流出不做硬过滤，但影响评分

        # ---- RPS 相对强弱 ----
        # 回测数据：升温+主升市场下 RPS50>=90 组 T+1胜率57.6%，<90组仅50.8%
        # 门槛从 70 提升到 90，确保只选市场真正认可的强势股
        rps50_val = float(rps50.get(ts_code, 50))
        rps120_val = float(rps120.get(ts_code, 50))
        if rps50_val >= 90:
            filters_passed.append(f"RPS50={rps50_val:.0f}")
        else:
            filters_failed.append(f"RPS50={rps50_val:.0f}<90")
            continue   # 硬过滤，RPS50不达标直接跳过

        # ---- 加分项 ----
        bonus_lhb = ts_code in lhb_codes
        bonus_north = north_net.get(ts_code, 0) > 0
        industry = str(row.get("industry", ""))
        bonus_event = industry in (event_catalyst_industries or set())
        if bonus_lhb:
            filters_passed.append("龙虎榜")
        if bonus_north:
            filters_passed.append("北向净买入")
        if bonus_event:
            filters_passed.append(f"⚡事件催化({industry})")

        # ---- 综合评分 ----
        score = _calc_total_score(
            rps50=rps50_val,
            rps120=rps120_val,
            main_net=main_net,
            pullback_score=pullback_score,
            is_macd_cross=is_macd_cross,
            above_ma60=above_ma60,
            ma20_rising=ma20_rising,
            bonus_lhb=bonus_lhb,
            bonus_north=bonus_north,
            bonus_event=bonus_event,
        )

        # ---- O1: RSI_14 ----
        rsi_series = rsi(close, n=14)
        rsi_14_val = float(rsi_series.iloc[-1]) if len(rsi_series) >= 14 else 50.0

        # ---- O2: VWAP 偏离率 ----
        vwap_dev = vwap_position(close, vol, n=20) if len(close) >= 20 else 0.0

        # ---- O3: 7日累计涨跌幅 ----
        change_7d = 0.0
        if len(close) >= 8:
            change_7d = float((close.iloc[-1] / close.iloc[-8] - 1) * 100)

        # ---- 走势摘要 ----
        from app.pattern_summary import generate_trend_summary
        high_s = high_m[ts_code].dropna().sort_index() if ts_code in high_m.columns else open_
        low_s = low_m[ts_code].dropna().sort_index() if ts_code in low_m.columns else open_
        trend_summary = generate_trend_summary(
            ts_code=ts_code,
            name=str(row.get("name", "")),
            close=close,
            open_=open_,
            high=high_s,
            low=low_s,
            vol=vol,
        )

        # ---- 交易执行计划（VWAP/止损/止盈/买点/仓位）----
        vwap_conservative, buy_aggressive = calc_buy_zones(close, vol)
        stop_loss_price = calc_stop_loss_price(close)
        tp1, tp2 = calc_take_profit_prices(close)

        results.append({
            "ts_code": ts_code,
            "name": row.get("name", ""),
            "trend_summary": trend_summary,
            "industry": row.get("industry", ""),
            "circ_mv_100m": row.get("circ_mv_100m", 0),
            "turnover_rate": turnover,
            "pct_chg": row.get("pct_chg", 0),
            "avg_amplitude": avg_amplitude,
            "main_net_amount": main_net,
            "rps50": rps50_val,
            "rps120": rps120_val,
            "pullback_score": pullback_score,
            "is_macd_cross": is_macd_cross,
            "bonus_lhb": bonus_lhb,
            "bonus_north": bonus_north,
            "total_score": score,
            "filters_passed": filters_passed,
            "filters_failed": filters_failed,
            # 交易计划字段
            "buy_conservative": vwap_conservative,
            "buy_aggressive": buy_aggressive,
            "stop_loss": stop_loss_price,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            # 次日观察清单（传入amount用于计算参考额）
            "amount": float(row.get("amount", 0)),
            # O1~O3 新因子
            "rsi_14": rsi_14_val,
            "vwap_deviation": round(vwap_dev * 100, 2),  # 转为百分比
            "change_pct_7d": round(change_7d, 2),
        })

    return pd.DataFrame(results) if results else pd.DataFrame()


def _calc_total_score(
    rps50: float,
    rps120: float,
    main_net: float,
    pullback_score: float,
    is_macd_cross: bool,
    above_ma60: bool,
    ma20_rising: bool,
    bonus_lhb: bool,
    bonus_north: bool,
    bonus_event: bool = False,
) -> float:
    """
    综合评分（0~100），权重分配：
    - RPS强度       30分（相对强弱最重要）
    - 主力资金      25分
    - 技术形态      25分（回踩质量 or MACD金叉）
    - 均线结构      10分
    - 加分项        10分（龙虎榜/北向/事件催化）

    事件驱动加分（+20，但不超过100上限）：对应板块有LLM确认的
    事件催化时触发，这是"唯一正期望策略"的核心权重（吴川回测）。
    """
    score = 0.0

    # RPS（30分）
    score += min(rps50 / 100 * 20, 20)
    score += min(rps120 / 100 * 10, 10)

    # 主力资金净流入（25分）
    if main_net > 50000:    # >5亿
        score += 25
    elif main_net > 10000:  # >1亿
        score += 18
    elif main_net > 0:
        score += 10

    # 技术形态（25分）
    score += min(pullback_score * 0.2, 20)
    if is_macd_cross:
        score += 5

    # 均线结构（10分）
    if above_ma60:
        score += 5
    if ma20_rising:
        score += 5

    # 加分项（最多30分：龙虎榜5+北向5+事件催化20）
    if bonus_lhb:
        score += 5
    if bonus_north:
        score += 5
    if bonus_event:
        score += 20  # 事件动量加分，对应吴川体系唯一正期望策略

    return round(min(score, 100.0), 2)


# ============================================================
# 辅助函数
# ============================================================

def _get_north_net_by_stock(north_flow: pd.DataFrame) -> dict:
    """
    北向资金是按日汇总的，不含个股明细（需要更高积分）。
    这里返回空字典，后续接个股北向数据时补充。
    """
    return {}


def _generate_execution_checklist(row: pd.Series, market_label: str) -> str:
    """
    生成次日 09:30-09:40 盘前观察清单（吴川体系T+1执行框架）。
    """
    name = row.get("name", "")
    close = float(row.get("buy_aggressive", 0))
    stop = float(row.get("stop_loss", 0))
    amount = float(row.get("amount", 0))  # 千元
    is_pullback = float(row.get("pullback_score", 0)) >= 50
    is_macd = bool(row.get("is_macd_cross", False))

    # 允许的开盘高开幅度（回踩型允许更低，突破型允许略高）
    max_high_open = 2.0 if is_pullback else 3.0
    # 前15分钟成交额参考（今日全天成交额的约15/240）
    ref_15min = amount * 15 / 240 / 10  # 千元→万元

    strategy = "缩量回踩低吸" if is_pullback else "MACD金叉突破"

    lines = [
        f"**{name} 次日盘前执行清单（{strategy}）**",
        f"⏰ 09:30-09:40 观察窗口：",
        f"  1. 开盘价：允许平开或小幅高开（<{max_high_open}%），"
        f"若高开>{max_high_open}%则等回踩{close:.2f}附近分时均线支撑后再看",
        f"  2. 前15分钟成交额 > {ref_15min:.0f}万元（今日同期水平），"
        f"确认有承接资金",
        f"  3. 大单方向：超大单/大单净流入为正",
        f"  4. 同行业个股不出现普跌",
        f"❌ 放弃条件：开盘价 < {stop:.2f}（止损价 -5%，直接放弃当日入场）"
        f" 或 高开>{max_high_open:.0f}%未回踩",
        f"✅ 若满足：先1/3仓试探，放量确认支撑后再加至{market_label}仓位上限",
    ]
    return "\n".join(lines)


def _build_candidate_objects(
    top: pd.DataFrame,
    stock_basic: pd.DataFrame,
    market_label: str = "震荡",
    event_catalyst_industries: set[str] | None = None,
) -> list[Candidate]:
    """将筛选结果转为 Candidate 对象列表，含完整交易执行计划。"""
    position_pct = calc_position_pct(market_label)
    candidates = []

    for _, row in top.iterrows():
        close_price = float(row.get("buy_aggressive", 0))
        factors = StockFactors(
            close=close_price,
            pct_change=float(row.get("pct_chg", 0)),
            turnover_rate=float(row.get("turnover_rate", 0)),
            market_cap=float(row.get("circ_mv_100m", 0)),
            fund_flow_3d=float(row.get("main_net_amount", 0)),
            avg_amplitude_5d=float(row.get("avg_amplitude", 0)),
            rps50=float(row.get("rps50", 0)),
            pullback_score=float(row.get("pullback_score", 0)),
            lhb_flag=bool(row.get("bonus_lhb", False)),
            comment_score=float(row.get("comment_score", 0)),
            popularity_rank=int(row.get("popularity_rank", 0)) if row.get("popularity_rank") else 0,
            main_cost=float(row.get("main_cost", 0)) if row.get("main_cost") else 0.0,
            # O1~O3
            rsi_14=float(row.get("rsi_14", 50)),
            vwap_deviation=float(row.get("vwap_deviation", 0)),
            change_pct_7d=float(row.get("change_pct_7d", 0)),
        )

        trade_plan = TradePlan(
            buy_conservative=float(row.get("buy_conservative", 0)),
            buy_aggressive=float(row.get("buy_aggressive", 0)),
            stop_loss=float(row.get("stop_loss", 0)),
            take_profit_1=float(row.get("take_profit_1", 0)),
            take_profit_2=float(row.get("take_profit_2", 0)),
            position_pct=position_pct,
            execution_checklist=_generate_execution_checklist(row, market_label),
        )

        c = Candidate(
            code=row["ts_code"],
            name=str(row.get("name", "")),
            theme=str(row.get("industry", "")),
            factors=factors,
            filters_passed=list(row.get("filters_passed", [])),
            filters_failed=list(row.get("filters_failed", [])),
            trend_summary=str(row.get("trend_summary", "")),
            trade_plan=trade_plan,
        )
        candidates.append(c)
    return candidates
