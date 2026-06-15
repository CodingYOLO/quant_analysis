"""
回测引擎（升级版）。

核心设计原则：
  - 无数据泄漏：选股只用 run_date 及之前的数据
  - 真实可执行价：买入用 T+1 开盘价，卖出用 T+N 收盘价
  - 逐笔保存因子：每只候选股的完整因子快照存入 strategy.db
  - 多时间窗口：T+1 / T+3 / T+5 一次回测同时计算
  - 止损检测：检查 T+1~T+N 区间内是否触及止损价（用每日 low 判断）

用法：
  # 纯量化（快，不调LLM，推荐用于回测）
  python -m app.run backtest --start 20251201 --end 20260612

  # 带LLM过滤（慢，接近实盘）
  python -m app.run backtest --start 20251201 --end 20260612 --use-llm
"""

import logging
from dataclasses import dataclass, field

import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.data.provider import DataProvider
from app.nodes.a_market_gate import _calc_market_regime
from app.nodes.c_stock_selection import _run_selection_pipeline
from app.state import Candidate
from app.strategy.db import (
    HORIZONS,
    PerformanceRecord,
    SelectionRecord,
    save_performances,
    save_selections,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 结果数据结构
# ──────────────────────────────────────────────

@dataclass
class HorizonStats:
    horizon: int
    total: int = 0
    wins: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_loss_ratio: float = 0.0
    max_loss: float = 0.0
    stop_rate: float = 0.0


@dataclass
class BacktestResult:
    start_date: str
    end_date: str
    use_llm: bool
    trade_dates_run: int = 0        # 实际跑过的交易日数
    skipped_dates: int = 0          # 弱势跳过的交易日数
    total_candidates: int = 0       # 量化选出的总候选股数
    llm_vetoed: int = 0             # LLM否决数
    horizons: dict[int, HorizonStats] = field(default_factory=dict)

    def summary(self) -> str:
        mode = "量化+LLM过滤" if self.use_llm else "纯量化"
        lines = [
            f"回测区间: {self.start_date} ~ {self.end_date}  模式: {mode}",
            f"运行交易日: {self.trade_dates_run}  弱势跳过: {self.skipped_dates}",
            f"量化候选股总笔数: {self.total_candidates}",
        ]
        if self.use_llm:
            lines.append(f"LLM否决: {self.llm_vetoed} 笔")
        lines.append("")
        lines.append(f"{'持仓':>6}  {'总笔':>6}  {'胜率':>7}  {'均收益':>8}  {'盈亏比':>7}  {'止损触发':>8}")
        lines.append("-" * 56)
        for h in sorted(self.horizons):
            s = self.horizons[h]
            if s.total == 0:
                continue
            lines.append(
                f"  T+{h}  {s.total:>6}  {s.win_rate:>6.1%}  "
                f"{s.avg_return:>+7.2%}  {s.profit_loss_ratio:>7.2f}  "
                f"{s.stop_rate:>7.1%}"
            )
        lines.append(f"\n对比基准（吴川体系）: T+1胜率 52.11%")
        return "\n".join(lines)


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

def run_backtest(
    start_date: str,
    end_date: str,
    use_llm: bool = False,
    save_to_db: bool = True,
) -> BacktestResult:
    """
    对历史区间每个交易日运行选股，计算 T+1/T+3/T+5 收益并存入 strategy.db。

    Args:
        start_date:  回测开始日（含）YYYYMMDD
        end_date:    回测结束日（含）YYYYMMDD
        use_llm:     是否启用 LLM 多空辩论过滤（默认关闭，快速模式）
        save_to_db:  是否将逐笔结果存入数据库（默认开启）
    """
    settings = get_settings()
    provider = CompositeProvider()
    result = BacktestResult(start_date=start_date, end_date=end_date, use_llm=use_llm)

    # 获取区间内所有交易日
    cal = provider.get_trade_cal(start_date, end_date)
    trade_dates: list[str] = sorted(cal[cal["is_open"] == 1]["cal_date"].tolist())

    # 多获取 end_date 之后 10 个交易日，用于计算 T+5 卖出价
    from datetime import datetime, timedelta
    extended_end = (datetime.strptime(end_date, "%Y%m%d") + timedelta(days=20)).strftime("%Y%m%d")
    cal_ext = provider.get_trade_cal(start_date, extended_end)
    all_dates: list[str] = sorted(cal_ext[cal_ext["is_open"] == 1]["cal_date"].tolist())

    logger.info(
        "回测: %s ~ %s，共 %d 个交易日，LLM=%s",
        start_date, end_date, len(trade_dates), use_llm,
    )

    # 按时间窗口收集收益列表（用于最终统计）
    horizon_returns: dict[int, list[float]] = {h: [] for h in HORIZONS}
    horizon_stops: dict[int, int] = {h: 0 for h in HORIZONS}

    for i, run_date in enumerate(trade_dates):
        # 找 run_date 在 all_dates 中的位置，用于定位 T+N 日期
        try:
            idx = all_dates.index(run_date)
        except ValueError:
            continue

        # 确保最大 T+5 的卖出日存在
        if idx + max(HORIZONS) >= len(all_dates):
            logger.debug("%s 后续交易日不足，跳过", run_date)
            continue

        # ── 市场择时 ──────────────────────────────
        try:
            regime = _calc_market_regime(run_date, provider)
        except Exception as e:
            logger.warning("%s 市场状态计算失败: %s", run_date, e)
            continue

        if not regime.can_open:
            result.skipped_dates += 1
            logger.debug("%s [%s] 跳过", run_date, regime.label)
            continue

        result.trade_dates_run += 1

        # ── 量化选股 ──────────────────────────────
        try:
            candidates = _run_selection_pipeline(
                trade_date=run_date,
                provider=provider,
                max_candidates=settings.max_candidates,
                min_market_cap=settings.min_market_cap,
                max_market_cap=settings.max_market_cap,
                market_label=regime.label,
            )
        except Exception as e:
            logger.warning("%s 选股失败: %s", run_date, e)
            continue

        if not candidates:
            continue

        result.total_candidates += len(candidates)

        # ── LLM 多空辩论过滤（可选）──────────────
        if use_llm:
            try:
                from app.data.history_loader import load_price_matrix
                from app.sector_analyzer import calc_sector_stats
                close_m, *_ = load_price_matrix(run_date, provider, n_days=25)
                sector_stats = calc_sector_stats(run_date, provider, close_m)
                decay_industries = {s.industry for s in sector_stats if s.phase == "退潮"}
            except Exception:
                decay_industries = set()

            candidates, vetoed = _apply_llm_filter(candidates, decay_industries)
            result.llm_vetoed += vetoed
            if not candidates:
                continue

        # ── 拉取 T+1 买入价 + T+1~T+5 日线（用于止损检测和卖出价）──
        buy_date = all_dates[idx + 1]   # T+1 开盘买入

        # 拉取 T+1 ~ T+5 的日线（含 low，用于止损检测）
        period_daily: dict[str, pd.DataFrame] = {}
        for h in HORIZONS:
            d = all_dates[idx + h]
            try:
                df = provider.get_daily(d)
                if df is not None and not df.empty:
                    period_daily[d] = df.set_index("ts_code")
            except Exception:
                pass

        buy_daily_df = period_daily.get(buy_date)
        if buy_daily_df is None:
            logger.debug("%s T+1日线缺失，跳过", run_date)
            continue

        # ── 逐股计算多时间窗口收益 ──────────────
        sel_records: list[SelectionRecord] = []
        perf_map: dict[str, dict[int, PerformanceRecord]] = {}  # ts_code -> horizon -> perf

        for c in candidates:
            code = c.code

            if code not in buy_daily_df.index:
                continue
            entry_price = float(buy_daily_df.loc[code, "open"])
            if entry_price <= 0:
                continue

            # 构建选股快照
            f = c.factors
            p = c.trade_plan
            rec = SelectionRecord(
                run_date=run_date,
                ts_code=code,
                name=c.name,
                theme=c.theme,
                market_label=regime.label,
                is_backtest=1,
                total_score=f.rps50,         # 用 rps50 作为主评分（total_score 在候选对象里未直接存）
                rps50=f.rps50,
                rsi_14=f.rsi_14,
                vwap_deviation=f.vwap_deviation,
                pullback_score=f.pullback_score,
                main_net_flow=f.fund_flow_3d,
                change_pct_7d=f.change_pct_7d,
                entry_price=entry_price,
                stop_loss=p.stop_loss,
                take_profit_1=p.take_profit_1,
                take_profit_2=p.take_profit_2,
            )
            sel_records.append(rec)
            perf_map[code] = {}

            # 为每个时间窗口计算收益
            for h in HORIZONS:
                sell_date = all_dates[idx + h]
                sell_df = period_daily.get(sell_date)
                if sell_df is None or code not in sell_df.index:
                    continue

                exit_price = float(sell_df.loc[code, "close"])
                if exit_price <= 0:
                    continue

                pct = (exit_price - entry_price) / entry_price * 100
                is_win = 1 if pct > 0 else 0

                # 止损检测：T+1 ~ T+N 期间 low 是否低于止损价
                hit_stop = 0
                if p.stop_loss > 0:
                    for hh in range(1, h + 1):
                        d = all_dates[idx + hh]
                        df = period_daily.get(d)
                        if df is not None and code in df.index:
                            low = float(df.loc[code, "low"])
                            if low <= p.stop_loss:
                                hit_stop = 1
                                break

                # 止盈1 检测
                hit_tp1 = 0
                if p.take_profit_1 > 0:
                    for hh in range(1, h + 1):
                        d = all_dates[idx + hh]
                        df = period_daily.get(d)
                        if df is not None and code in df.index:
                            high = float(df.loc[code, "high"])
                            if high >= p.take_profit_1:
                                hit_tp1 = 1
                                break

                perf_map[code][h] = PerformanceRecord(
                    selection_id=0,   # 写入 DB 后回填
                    horizon=h,
                    eval_date=sell_date,
                    exit_price=exit_price,
                    pct_return=round(pct, 4),
                    is_win=is_win,
                    hit_stop_loss=hit_stop,
                    hit_take_profit_1=hit_tp1,
                )

                horizon_returns[h].append(pct)
                if hit_stop:
                    horizon_stops[h] += 1

        # ── 写入数据库 ──────────────────────────
        if save_to_db and sel_records:
            sel_ids = save_selections(sel_records)
            perf_records: list[PerformanceRecord] = []
            for sel_id, rec in zip(sel_ids, sel_records):
                for h, perf in perf_map.get(rec.ts_code, {}).items():
                    perf.selection_id = sel_id
                    perf_records.append(perf)
            if perf_records:
                save_performances(perf_records)

        logger.info(
            "%s [%s] 选出 %d 只 → 写入 %d 笔",
            run_date, regime.label, len(candidates), len(sel_records),
        )

    # ── 汇总统计 ────────────────────────────────
    for h in HORIZONS:
        rets = horizon_returns[h]
        if not rets:
            result.horizons[h] = HorizonStats(horizon=h)
            continue
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        result.horizons[h] = HorizonStats(
            horizon=h,
            total=len(rets),
            wins=len(wins),
            win_rate=len(wins) / len(rets),
            avg_return=sum(rets) / len(rets) / 100,
            avg_win=avg_win / 100,
            avg_loss=avg_loss / 100,
            profit_loss_ratio=abs(avg_win / avg_loss) if avg_loss != 0 else float("inf"),
            max_loss=min(rets) / 100,
            stop_rate=horizon_stops[h] / len(rets),
        )

    return result


# ──────────────────────────────────────────────
# LLM 过滤（可选，与真实 pipeline 保持一致）
# ──────────────────────────────────────────────

def _apply_llm_filter(
    candidates: list[Candidate],
    decay_industries: set[str],
) -> tuple[list[Candidate], int]:
    """调用 LLM 多空辩论过滤，返回 (通过列表, 否决数)。"""
    from app.nodes.d_risk_debate import _check_hard_rules, _llm_debate

    passed, vetoed = [], 0
    for c in candidates:
        hard_veto, reason = _check_hard_rules(c, decay_industries)
        if hard_veto:
            vetoed += 1
            continue
        try:
            _, _, risk_level, soft_veto, _ = _llm_debate(c, decay_industries)
            in_decay = c.theme in decay_industries
            if soft_veto or (risk_level == "high" and in_decay):
                vetoed += 1
            else:
                passed.append(c)
        except Exception:
            passed.append(c)

    return passed, vetoed
