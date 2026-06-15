"""
节点 A: 市场择时 Gate（吴川体系第一层）。

判断逻辑：
  强势市场（绿灯）：涨停≥80，跌停<15，下跌家数<2000，大盘股MA5占比>60%
  震荡市场（黄灯）：介于强势和弱势之间
  弱势市场（红灯）：下跌家数>3000 或 跌停占比>1.2%（≈66家/5500只）

仓位上限：强势0.6，震荡0.3，弱势0.0（不开仓）
"""

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.factors import ma, above_ma
from app.state import MarketRegime, PipelineState

logger = logging.getLogger(__name__)

# 判断涨跌停的阈值（A股非ST股涨跌幅限制 ±10%，ST ±5%）
_LIMIT_UP_PCT = 9.5
_LIMIT_DOWN_PCT = -9.5


def node_market_gate(state: PipelineState) -> PipelineState:
    """市场择时：根据全市场当日行情判断大盘状态与仓位上限。"""
    provider = CompositeProvider()
    regime = _calc_market_regime(state.trade_date, provider)
    state.market_regime = regime

    logger.info(
        "[节点A] 大盘状态=%s 可开仓=%s 仓位上限=%.0f%% 涨停=%d 跌停=%d 下跌家数=%s",
        regime.label, regime.can_open, regime.position_limit * 100,
        regime.limit_up_count, regime.limit_down_count, regime.down_count,
    )
    return state


def _calc_market_regime(trade_date: str, provider: CompositeProvider) -> MarketRegime:
    """核心计算：拉取当日全市场数据，输出市场状态。"""
    daily = provider.get_daily(trade_date)
    if daily is None or daily.empty:
        return MarketRegime(
            label="数据缺失",
            can_open=False,
            position_limit=0.0,
            reason=f"{trade_date} 无行情数据，可能为非交易日",
        )

    # ---- 1. 涨跌停统计 ----
    limit_up = int((daily["pct_chg"] >= _LIMIT_UP_PCT).sum())
    limit_down = int((daily["pct_chg"] <= _LIMIT_DOWN_PCT).sum())
    down_count = int((daily["pct_chg"] < 0).sum())
    total = len(daily)

    # ---- 2. 最高连板高度（用当日涨停股的涨幅排名粗估）----
    # 真正的连板高度需要多日数据，这里用当日涨停家数作为情绪替代指标
    consecutive_high = _estimate_consecutive_high(trade_date, provider)

    # ---- 3. 大盘股站上 MA5 的占比 ----
    daily_basic = provider.get_daily_basic(trade_date)
    pct_above_ma5 = _calc_pct_above_ma(trade_date, provider, n=5)
    pct_above_ma20 = _calc_pct_above_ma(trade_date, provider, n=20)

    # ---- 4. 指数 MA 状态（沪深300） ----
    hs300_above_ma5, hs300_above_ma20 = _calc_index_ma_status(trade_date, provider)

    # ---- 5. 北向资金 ----
    north_net = _get_north_net(trade_date, provider)

    # ---- 6. 综合判断 ----
    label, can_open, position_limit, reason = _determine_regime(
        limit_up=limit_up,
        limit_down=limit_down,
        down_count=down_count,
        total=total,
        pct_above_ma5=pct_above_ma5,
        hs300_above_ma5=hs300_above_ma5,
        hs300_above_ma20=hs300_above_ma20,
        north_net=north_net,
    )

    confidence = _calc_confidence(
        label=label,
        pct_above_ma5=pct_above_ma5,
        limit_up=limit_up,
        limit_down=limit_down,
        down_count=down_count,
        total=total,
    )
    emotion_score = _calc_emotion_score(
        limit_up=limit_up,
        limit_down=limit_down,
        pct_above_ma5=pct_above_ma5,
        consecutive_high=consecutive_high,
        north_net=north_net,
    )

    return MarketRegime(
        label=label,
        can_open=can_open,
        position_limit=position_limit,
        limit_up_count=limit_up,
        limit_down_count=limit_down,
        down_count=down_count,
        consecutive_limit_high=consecutive_high,
        pct_above_ma5=pct_above_ma5,
        pct_above_ma20=pct_above_ma20,
        hs300_above_ma5=hs300_above_ma5,
        hs300_above_ma20=hs300_above_ma20,
        north_net_million=north_net,
        reason=reason,
        confidence=confidence,
        emotion_score=emotion_score,
    )


def _determine_regime(
    limit_up: int,
    limit_down: int,
    down_count: int,
    total: int,
    pct_above_ma5: float,
    hs300_above_ma5: bool,
    hs300_above_ma20: bool,
    north_net: float,
) -> tuple[str, bool, float, str]:
    """
    吴川体系4态判断：衰退→弱势→退潮反抽→震荡→升温→主升

    衰退：站上MA5<20% 或 跌停>50 → 不开仓
    弱势：下跌家数>3000 或 跌停>30 → 不开仓
    退潮反抽：站上MA5 30-50% + 连板≤4板 + 涨停<100 → 仓位20%
    震荡：无明确方向 → 仓位30%
    升温：站上MA5>60% + 涨停≥80 + 跌停<15 → 仓位50%
    主升：站上MA5>70% + 涨停≥120 + 北向净流入 → 仓位60%
    """
    # ---- 衰退（最危险，优先判断）----
    if pct_above_ma5 < 0.20 or limit_down > 50:
        return "衰退", False, 0.0, f"站上MA5仅{pct_above_ma5:.1%}，市场极度恐慌，停止交易"

    # ---- 弱势 ----
    # 跌停阈值用比例制：A股5500+只股票，绝对数30已失效，改为占全市场1.2%
    limit_down_ratio = limit_down / total if total > 0 else 0
    if down_count > 3000:
        return "弱势", False, 0.0, f"全市场下跌{down_count}家>3000，暂停开仓"
    if limit_down_ratio > 0.012:
        return "弱势", False, 0.0, (
            f"跌停{limit_down}家（占比{limit_down_ratio:.1%}）>1.2%，市场恐慌，暂停开仓"
        )

    # ---- 主升（最强势）----
    if (pct_above_ma5 > 0.70 and limit_up >= 120
            and limit_down < 10 and north_net > 0):
        return "主升", True, 0.6, (
            f"站上MA5 {pct_above_ma5:.1%}，涨停{limit_up}家，"
            f"跌停{limit_down}家，北向净流入，市场主升格局"
        )

    # ---- 升温 ----
    if pct_above_ma5 > 0.60 and limit_up >= 80 and limit_down < 15:
        return "升温", True, 0.5, (
            f"站上MA5 {pct_above_ma5:.1%}，涨停{limit_up}家，"
            f"跌停{limit_down}家，市场情绪升温"
        )

    # ---- 退潮反抽（吴川文档精确描述的状态）----
    # 特征：MA5占比30-50% + 涨停<100 + 有反弹但连板低
    # 回测数据：T+1胜率仅20%，暂停实盘开仓，进入观察模式
    is_rebound = (
        0.30 <= pct_above_ma5 <= 0.55
        and limit_up < 100
        and down_count < 2500
    )
    if is_rebound:
        return "退潮反抽", False, 0.0, (
            f"站上MA5 {pct_above_ma5:.1%}（30-55%区间），"
            f"涨停{limit_up}家<100，历史T+1胜率仅20%，观察模式不开仓"
        )

    # ---- 震荡（兜底）----
    # 回测数据：T+1胜率43.5%，低于随机基准，暂停实盘开仓，进入观察模式
    reasons = []
    if limit_up >= 50:
        reasons.append(f"涨停{limit_up}家")
    if hs300_above_ma5:
        reasons.append("沪深300站上MA5")
    else:
        reasons.append("沪深300未站上MA5")
    reasons.append(f"站上MA5占比{pct_above_ma5:.1%}")
    reasons.append("历史T+1胜率43.5%，观察模式不开仓")
    return "震荡", False, 0.0, "，".join(reasons)


def _calc_pct_above_ma(trade_date: str, provider: CompositeProvider, n: int) -> float:
    """计算全市场个股站上 N 日均线的比例。使用最近 n+1 日数据。"""
    try:
        from app.data.history_loader import _get_prior_dates, load_price_matrix
        close_m, *_ = load_price_matrix(trade_date, provider, n_days=n + 5)
        if len(close_m) < n + 1:
            return 0.5
        recent = close_m.tail(n + 1)
        ma_vals = recent.iloc[:-1].mean()  # 简单均线
        last_close = recent.iloc[-1]
        above = (last_close > ma_vals).sum()
        total = last_close.notna().sum()
        return float(above / total) if total > 0 else 0.5
    except Exception as e:
        logger.warning("计算MA%d占比失败: %s", n, e)
        return 0.5


def _calc_index_ma_status(trade_date: str, provider: CompositeProvider) -> tuple[bool, bool]:
    """判断沪深300是否站上 MA5 和 MA20。"""
    try:
        df = provider.get_index_daily("399300.SZ", trade_date)
        if df is None or len(df) < 22:
            return False, False
        df = df.sort_values("trade_date")
        close = df["close"]
        return above_ma(close, 5), above_ma(close, 20)
    except Exception as e:
        logger.warning("获取指数MA状态失败: %s", e)
        return False, False


def _get_north_net(trade_date: str, provider: CompositeProvider) -> float:
    """获取北向资金净买入额（万元），正值=净流入。"""
    try:
        df = provider.get_north_flow(trade_date)
        if df is None or df.empty:
            return 0.0
        return float(df["north_money"].iloc[0])
    except Exception as e:
        logger.warning("获取北向资金失败: %s", e)
        return 0.0


def _estimate_consecutive_high(trade_date: str, provider: CompositeProvider) -> int:
    """
    精确计算全市场最高连板高度。
    拉最近5个交易日的全市场日线，找连续N天涨幅≥9.5%的最大N值。
    """
    try:
        from app.data.history_loader import _get_prior_dates
        dates = _get_prior_dates(trade_date, provider, n=5)
        if len(dates) < 2:
            return 0

        frames = []
        for d in dates:
            df = provider.get_daily(d)
            if df is None or df.empty:
                continue
            df["_date"] = d
            frames.append(df[["ts_code", "pct_chg", "_date"]])

        if not frames:
            return 0

        all_daily = pd.concat(frames, ignore_index=True)
        dates_sorted = sorted(dates)
        max_consec = 0

        for ts_code, grp in all_daily.groupby("ts_code"):
            grp = grp.set_index("_date")["pct_chg"].reindex(dates_sorted).fillna(0)
            count = 0
            for d in reversed(dates_sorted):
                if grp.get(d, 0) >= _LIMIT_UP_PCT:
                    count += 1
                else:
                    break
            if count > max_consec:
                max_consec = count

        return max_consec

    except Exception as e:
        logger.warning("计算连板高度失败，回退粗估: %s", e)
        return _fallback_consecutive_high(trade_date, provider)


def _calc_confidence(
    label: str,
    pct_above_ma5: float,
    limit_up: int,
    limit_down: int,
    down_count: int,
    total: int,
) -> float:
    """
    判断置信度（0~1）：多指标共识度越高，置信度越高。
    边界模糊区域（如 MA5占比恰好50%附近）置信度低。
    """
    # 极端状态置信度高
    if pct_above_ma5 < 0.20 or pct_above_ma5 > 0.75:
        return 0.90
    if limit_down > 50 or limit_up > 150:
        return 0.90
    # 边界区域置信度低（MA5占比 45%-65% 是最模糊的区间）
    if 0.40 <= pct_above_ma5 <= 0.65:
        return round(0.50 + abs(pct_above_ma5 - 0.525) * 2, 2)
    return 0.75


def _calc_emotion_score(
    limit_up: int,
    limit_down: int,
    pct_above_ma5: float,
    consecutive_high: int,
    north_net: float,
) -> float:
    """
    市场情绪综合分 0~100（越高越乐观）。
    权重：涨停家数30%+MA5占比30%+连板高度20%+北向资金10%+跌停惩罚10%
    """
    # 涨停家数（200家以上=满分）
    limit_up_score = min(limit_up / 200, 1.0) * 100

    # MA5占比（0%~100%）
    ma5_score = pct_above_ma5 * 100

    # 连板高度（10板以上=满分）
    consec_score = min(consecutive_high / 10, 1.0) * 100

    # 北向资金（净流入>50亿万元=满分，净流出=0分）
    north_score = max(0, min(north_net / 500000, 1.0)) * 100

    # 跌停惩罚（0家=满分，50家以上=0分）
    down_penalty = max(0, 1.0 - limit_down / 50) * 100

    score = (
        limit_up_score * 0.30
        + ma5_score * 0.30
        + consec_score * 0.20
        + north_score * 0.10
        + down_penalty * 0.10
    )
    return round(score, 1)


def _fallback_consecutive_high(trade_date: str, provider: CompositeProvider) -> int:
    """连板精确计算失败时的粗估备用方案。"""
    try:
        daily = provider.get_daily(trade_date)
        if daily is None or daily.empty:
            return 0
        limit_up_count = int((daily["pct_chg"] >= _LIMIT_UP_PCT).sum())
        if limit_up_count > 150:
            return 5
        elif limit_up_count > 100:
            return 4
        elif limit_up_count > 50:
            return 3
        return 2
    except Exception:
        return 0
