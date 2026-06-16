"""
M2：中枢宽表 theme_heat_all_in_one 计算引擎（行业口径）。

复用优先：
  - 已有因子（heat_score/phase/tier/资金3·5日/MA20广度/集中度/次日风险/delta）→ 复用 sector_analyzer.calc_sector_stats。
  - 前复权多档均线广度 → 复用 M1 breadth_qfq。
  - 新增：多周期资金(1/7日)、多周期涨跌(等权)、Top100/300、money_flow_3d_norm、sample_reliability。
  - 人气体系字段 → 暂置 None（M3 东财人气榜前向积累后填充）。

数据走 CompositeProvider；落库走 theme_heat_db。概念口径见 M2.5（成分走 ths_member）。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix
from app.data.theme_heat_db import ThemeWideRow, upsert_rows
from app.factors.breadth_qfq import BREADTH_WINDOWS, build_qfq_panel, compute_breadth, _recent_trade_dates
from app.sector_analyzer import calc_sector_stats

logger = logging.getLogger(__name__)

# 决策 → tier 映射（已有 decision 即 buy/watch/avoid，与 PRD tier 同义）
_TIER = {"buy": "buy", "watch": "watch", "avoid": "avoid"}


def build_industry_wide(
    trade_date: str,
    provider: CompositeProvider | None = None,
    k_norm: float = 1.0,
    persist: bool = True,
    lookback: int = 145,
) -> list[ThemeWideRow]:
    """
    计算指定交易日全部行业的宽表行。

    Args:
        trade_date: 交易日 YYYYMMDD
        provider:   数据接口
        k_norm:     money_flow_3d_norm 的系数 k【需校准 C2】
        persist:    是否落库 theme_heat.db

    Returns:
        ThemeWideRow 列表。
    """
    provider = provider or CompositeProvider()

    # ---- 1. 复用已有因子 ----
    close_m, *_ = load_price_matrix(trade_date, provider, n_days=25)
    if close_m is None or close_m.empty:
        logger.warning("[宽表] %s 价格矩阵为空", trade_date)
        return []
    stats = calc_sector_stats(trade_date, provider, close_m)
    if not stats:
        return []

    # ---- 2. 行业成分映射 ----
    sb = provider.get_stock_basic()
    ind_members: dict[str, list[str]] = {
        str(ind): grp["ts_code"].tolist()
        for ind, grp in sb.dropna(subset=["industry"]).groupby("industry")
    }

    # ---- 3. 前复权面板（多档广度 + 多周期复权收益）----
    panel = build_qfq_panel(trade_date, provider, lookback=lookback)

    # ---- 4. 多周期资金（近7个交易日）+ Top100/300（当日主力净流入）----
    dates7 = _recent_trade_dates(provider, trade_date, 7)
    mf_by_date = _load_moneyflow(provider, dates7)
    code2net_by_date = {d: _net_map(mf) for d, mf in mf_by_date.items()}
    top100, top300 = _top_rank_sets(code2net_by_date.get(trade_date, {}))

    # 人气体系（M3）：仅当日有东财人气榜数据时非空，否则各 pop_* 为 None
    from app.factors.popularity import get_intraday_weights
    pop_weights = get_intraday_weights(trade_date)

    rows: list[ThemeWideRow] = []
    for st in stats:
        codes = ind_members.get(st.industry, [])
        rows.append(_build_one_row(
            st, codes, trade_date, panel,
            dates7, code2net_by_date, top100, top300, k_norm, pop_weights,
        ))

    if persist:
        upsert_rows(rows)
        logger.info("[宽表] %s 行业宽表写入 %d 行", trade_date, len(rows))
    return rows


# ──────────────────────────────────────────────
# 单行组装
# ──────────────────────────────────────────────

def _build_one_row(
    st, codes, trade_date, panel, dates7, code2net_by_date, top100, top300, k_norm, pop_weights,
) -> ThemeWideRow:
    n = len(codes)
    breadth = compute_breadth(panel, codes, BREADTH_WINDOWS) if n else {f"ma{w}": None for w in BREADTH_WINDOWS}
    money = _multi_period_money(codes, dates7, code2net_by_date)
    pct = _multi_period_return(codes, panel)
    cset = set(codes)
    top100_ratio = round(len(cset & top100) / n * 100, 1) if n else None
    top300_ratio = round(len(cset & top300) / n * 100, 1) if n else None
    norm = round(money["3d"] / (n ** 0.5) * k_norm, 2) if (money["3d"] is not None and n) else None
    reliability = _sample_reliability(codes, code2net_by_date.get(trade_date, {}))

    # 人气体系（数据缺失时各项为 None）
    from app.factors.popularity import theme_pop_factors
    pop = theme_pop_factors(codes, pop_weights)

    return ThemeWideRow(
        theme_name=st.industry,
        trade_date=trade_date,
        theme_type="industry",
        sample_count=n,
        sample_reliability=reliability,
        money_flow_1d=money["1d"], money_flow_3d=money["3d"],
        money_flow_5d=money["5d"], money_flow_7d=money["7d"],
        money_flow_3d_norm=norm,
        pct_chg_1d=pct["1d"], pct_chg_3d=pct["3d"], pct_chg_5d=pct["5d"], pct_chg_7d=pct["7d"],
        breadth_ma3=breadth["ma3"], breadth_ma5=breadth["ma5"], breadth_ma10=breadth["ma10"],
        breadth_ma20=breadth["ma20"], breadth_ma30=breadth["ma30"], breadth_ma60=breadth["ma60"],
        breadth_ma90=breadth["ma90"], breadth_ma144=breadth["ma144"],
        top100_ratio=top100_ratio, top300_ratio=top300_ratio,
        pop_weight=pop["pop_weight"],
        pop_concentration_hhi=pop["pop_concentration_hhi"],
        pop_fairness=pop["pop_fairness"],
        heat_score=round(st.heat_score, 1),
        heat_score_delta_3d=round(st.heat_score_delta_3d, 1),
        trend=_trend_label(st),
        phase=st.phase,
        tier=_TIER.get(st.decision, st.decision),
        nextday_risk_penalty=round(st.nextday_risk_penalty, 1),
        pop_concentration_amount=round(st.pop_concentration, 4),
    )


# ──────────────────────────────────────────────
# 新增因子计算
# ──────────────────────────────────────────────

def _load_moneyflow(provider, dates: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for d in dates:
        try:
            mf = provider.get_money_flow(d)
            if mf is not None and not mf.empty:
                out[d] = mf
        except Exception as e:
            logger.debug("[宽表] %s 资金流失败: %s", d, e)
    return out


def _net_map(mf: pd.DataFrame) -> dict[str, float]:
    """ts_code → 当日主力净流入（万元）。沿用 sector_analyzer 的 net_mf_amount 口径。"""
    if "net_mf_amount" not in mf.columns:
        return {}
    s = pd.to_numeric(mf["net_mf_amount"], errors="coerce")
    return dict(zip(mf["ts_code"], s))


def _multi_period_money(codes, dates7, code2net_by_date) -> dict[str, float | None]:
    """多周期资金净流入（亿）：净流入(万元)求和 / 1e4。"""
    dates_sorted = sorted(dates7)
    cset = set(codes)

    def window_sum(n: int) -> float | None:
        wd = dates_sorted[-n:]
        if not wd:
            return None
        total = 0.0
        for d in wd:
            nm = code2net_by_date.get(d, {})
            total += sum(v for c, v in nm.items() if c in cset and pd.notna(v))
        return round(total / 1e4, 2)

    return {"1d": window_sum(1), "3d": window_sum(3), "5d": window_sum(5), "7d": window_sum(7)}


def _multi_period_return(codes, panel: pd.DataFrame) -> dict[str, float | None]:
    """多周期等权涨跌（%）：基于前复权面板的 N 日收益均值。"""
    if panel is None or panel.empty or not codes:
        return {k: None for k in ("1d", "3d", "5d", "7d")}
    sub = panel.reindex(codes)
    cols = list(panel.columns)
    cur = sub[cols[-1]]

    def ret_n(n: int) -> float | None:
        if len(cols) <= n:
            return None
        base = sub[cols[-1 - n]]
        valid = cur.notna() & base.notna() & (base > 0)
        if valid.sum() == 0:
            return None
        return round(float(((cur[valid] / base[valid]) - 1).mean()) * 100, 2)

    return {"1d": ret_n(1), "3d": ret_n(3), "5d": ret_n(5), "7d": ret_n(7)}


def _top_rank_sets(today_net: dict[str, float]) -> tuple[set, set]:
    """全市场按当日主力净流入降序，取前 100 / 前 300 ts_code 集合。"""
    if not today_net:
        return set(), set()
    ranked = sorted(today_net.items(), key=lambda kv: (kv[1] if pd.notna(kv[1]) else -1e18), reverse=True)
    codes = [c for c, _ in ranked]
    return set(codes[:100]), set(codes[:300])


def _sample_reliability(codes, today_net: dict[str, float]) -> float | None:
    """成分股中当日有资金流数据的占比（%），反映样本质控。"""
    if not codes:
        return None
    have = sum(1 for c in codes if c in today_net and pd.notna(today_net[c]))
    return round(have / len(codes) * 100, 1)


def _trend_label(st) -> str:
    """趋势标签：依据 3 日热度变化派生 new/up/down/flat。"""
    d = st.heat_score_delta_3d
    if d == 0:
        return "new"        # 冷启动无历史热度
    if d > 3:
        return "up"
    if d < -3:
        return "down"
    return "flat"
