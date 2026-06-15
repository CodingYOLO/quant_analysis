"""
前向追踪器：管理 pipeline 实盘选股的保存与多时间窗口回填。

时间线（以 20260612 选股为例）：
  20260612 pipeline 运行 → 保存快照，entry_price=0（次日开盘价未知）
  20260613 pipeline 运行 → 回填 20260612 的 entry_price(T+1 open) + T+1 收益
  20260617 pipeline 运行 → 回填 20260612 的 T+3 收益（3个交易日后）
  20260619 pipeline 运行 → 回填 20260612 的 T+5 收益

关键：
  - 买入价 = 次交易日开盘价（T+1 open），不是选股当日收盘价
  - 卖出价 = 持仓到期日收盘价（T+N close）
  - entry_price = 0 表示"快照已存，买入价待回填"
  - 止损检测用每日 low，止盈检测用每日 high
"""

import logging
from datetime import datetime

from app.data.provider import DataProvider
from app.state import Candidate
from app.strategy.db import (
    HORIZONS,
    PerformanceRecord,
    SelectionRecord,
    get_pending_selections,
    save_performances,
    save_selections,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 保存今日候选股快照（entry_price 暂为 0）
# ──────────────────────────────────────────────

def save_today_candidates(
    trade_date: str,
    candidates: list[Candidate],
    market_label: str,
) -> int:
    """
    将今日通过辩论的候选股写入 selection_records（is_backtest=0）。
    entry_price 此时为 0，等次日回填。

    Args:
        trade_date:   今日交易日 YYYYMMDD
        candidates:   通过多空辩论的候选股列表
        market_label: 今日市场状态（强势/震荡/弱势）

    Returns:
        写入成功的条数
    """
    if not candidates:
        return 0

    records = [
        SelectionRecord(
            run_date=trade_date,
            ts_code=c.code,
            name=c.name,
            theme=c.theme,
            market_label=market_label,
            is_backtest=0,
            rps50=c.factors.rps50,
            rsi_14=c.factors.rsi_14,
            vwap_deviation=c.factors.vwap_deviation,
            pullback_score=c.factors.pullback_score,
            main_net_flow=c.factors.fund_flow_3d,
            change_pct_7d=c.factors.change_pct_7d,
            entry_price=0.0,          # 次日开盘价未知，暂存 0
            stop_loss=c.trade_plan.stop_loss,
            take_profit_1=c.trade_plan.take_profit_1,
            take_profit_2=c.trade_plan.take_profit_2,
        )
        for c in candidates
    ]

    ids = save_selections(records)
    logger.info("[前向追踪] 保存 %d 条候选股快照，交易日=%s", len(ids), trade_date)
    return len(ids)


# ──────────────────────────────────────────────
# 回填历史未完成记录
# ──────────────────────────────────────────────

def backfill_forward(
    today: str,
    provider: DataProvider,
    all_trade_dates: list[str],
) -> dict[str, int]:
    """
    在每次 pipeline 启动时调用，用今日及过去若干日的价格数据，
    回填所有未完成的前向追踪记录。

    Args:
        today:            今日交易日 YYYYMMDD
        provider:         数据接口
        all_trade_dates:  按升序排列的交易日列表（用于计算 T+N 日期）

    Returns:
        {'entry_filled': N, 't1': N, 't3': N, 't5': N}
    """
    pending = get_pending_selections(is_backtest=0)
    if not pending:
        return {}

    # 预加载今日日线（用于多数回填场景）
    daily_cache: dict[str, object] = {}

    def get_daily(date: str):
        if date not in daily_cache:
            try:
                df = provider.get_daily(date)
                daily_cache[date] = df.set_index("ts_code") if df is not None and not df.empty else None
            except Exception:
                daily_cache[date] = None
        return daily_cache[date]

    stats = {"entry_filled": 0, "t1": 0, "t3": 0, "t5": 0}
    perf_records: list[PerformanceRecord] = []
    # 需要更新 entry_price 的记录（selection_id → new entry_price）
    entry_updates: list[tuple[float, int]] = []

    for rec in pending:
        run_date = rec["run_date"]
        sel_id = rec["id"]
        code = rec["ts_code"]
        stop_loss = rec.get("stop_loss") or 0.0
        take_profit_1 = rec.get("take_profit_1") or 0.0
        entry_price = rec.get("entry_price") or 0.0

        # 找 run_date 在 all_trade_dates 中的索引
        if run_date not in all_trade_dates:
            continue
        idx = all_trade_dates.index(run_date)

        # ── Step A: 回填 entry_price（T+1 open）──────────────────
        if entry_price == 0:
            if idx + 1 >= len(all_trade_dates):
                continue
            t1_date = all_trade_dates[idx + 1]
            if t1_date > today:
                continue   # 次日还没到
            df = get_daily(t1_date)
            if df is not None and code in df.index:
                entry_price = float(df.loc[code, "open"])
                if entry_price > 0:
                    entry_updates.append((entry_price, sel_id))
                    stats["entry_filled"] += 1

        if entry_price <= 0:
            continue   # 买入价还不知道，后续收益无法计算

        # ── Step B: 回填各时间窗口收益 ──────────────────────────
        for h in rec.get("pending_horizons", HORIZONS):
            if idx + h >= len(all_trade_dates):
                continue
            eval_date = all_trade_dates[idx + h]
            if eval_date > today:
                continue   # 卖出日还没到

            df = get_daily(eval_date)
            if df is None or code not in df.index:
                continue

            exit_price = float(df.loc[code, "close"])
            if exit_price <= 0:
                continue

            pct = (exit_price - entry_price) / entry_price * 100

            # 止损 / 止盈检测：遍历 T+1 ~ T+h 的每日 low/high
            hit_stop, hit_tp1 = 0, 0
            for hh in range(1, h + 1):
                if idx + hh >= len(all_trade_dates):
                    break
                d = all_trade_dates[idx + hh]
                if d > today:
                    break
                df_d = get_daily(d)
                if df_d is None or code not in df_d.index:
                    continue
                if stop_loss > 0 and float(df_d.loc[code, "low"]) <= stop_loss:
                    hit_stop = 1
                if take_profit_1 > 0 and float(df_d.loc[code, "high"]) >= take_profit_1:
                    hit_tp1 = 1

            perf_records.append(PerformanceRecord(
                selection_id=sel_id,
                horizon=h,
                eval_date=eval_date,
                exit_price=round(exit_price, 4),
                pct_return=round(pct, 4),
                is_win=1 if pct > 0 else 0,
                hit_stop_loss=hit_stop,
                hit_take_profit_1=hit_tp1,
            ))
            stats[f"t{h}"] = stats.get(f"t{h}", 0) + 1

    # 批量写入
    if entry_updates:
        from app.strategy.db import _conn
        with _conn() as con:
            con.executemany(
                "UPDATE selection_records SET entry_price=? WHERE id=?",
                entry_updates,
            )

    if perf_records:
        save_performances(perf_records)

    filled_total = sum(v for k, v in stats.items() if k != "entry_filled")
    if stats["entry_filled"] or filled_total:
        logger.info(
            "[前向追踪] 回填完成: entry=%d  T1=%d  T3=%d  T5=%d",
            stats["entry_filled"],
            stats.get("t1", 0),
            stats.get("t3", 0),
            stats.get("t5", 0),
        )
    return stats


# ──────────────────────────────────────────────
# 生成报告区块（展示当日追踪状态）
# ──────────────────────────────────────────────

def get_tracking_report_section(trade_date: str) -> str:
    """
    生成追踪状态的 Markdown 区块，嵌入每日报告末尾。
    只展示"近 10 个交易日内"还在观察窗口内的记录。
    """
    from app.strategy.db import get_all_with_performance

    records = get_all_with_performance(is_backtest=0)
    if not records:
        return ""

    # 只显示近 10 个交易日的记录
    recent = [r for r in records if r["run_date"] >= _offset_date(trade_date, -14)]
    if not recent:
        return ""

    lines = ["", "## 📊 前向追踪（近期选股实际表现）", ""]
    lines += [
        "| 名称 | 代码 | 选股日 | 市场 | 买入价 | T+1收益 | T+3收益 | T+5收益 | 止损触发 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for r in recent[:20]:  # 最多展示 20 条
        def fmt_ret(val, win):
            if val is None:
                return "—"
            color = "+" if val >= 0 else ""
            mark = "✅" if win else "❌"
            return f"{mark}{color}{val:.1f}%"

        stop_str = "🛑" if r.get("t1_stop") else "—"
        entry = f"{r['entry_price']:.2f}" if r.get("entry_price") and r["entry_price"] > 0 else "待回填"

        lines.append(
            f"| {r['name']} | {r['ts_code'][:6]} | {r['run_date'][:4]}-{r['run_date'][4:6]}-{r['run_date'][6:]} "
            f"| {r['market_label']} | {entry} "
            f"| {fmt_ret(r.get('t1_return'), r.get('t1_win'))} "
            f"| {fmt_ret(r.get('t3_return'), r.get('t3_win'))} "
            f"| {fmt_ret(r.get('t5_return'), r.get('t5_win'))} "
            f"| {stop_str} |"
        )

    return "\n".join(lines)


def get_recent_watchlist_perf(trade_date: str, days: int = 5) -> list[dict]:
    """
    读取近 N 日观察池个股的实盘追踪记录，供盘后复盘展示。

    Args:
        trade_date: 当日交易日（YYYYMMDD），用于过滤时间范围
        days: 往前查几个自然日（默认5）

    Returns:
        list of dict，每条含: name, code, run_date, market_label,
        stop_loss, pct_return(T+1), hit_stop_loss
    """
    from app.strategy.db import get_all_with_performance

    cutoff = _offset_date(trade_date, -days * 2)  # 乘2保证覆盖节假日
    records = get_all_with_performance(is_backtest=0)
    recent = [r for r in records if r["run_date"] >= cutoff]

    result = []
    for r in recent:
        result.append({
            "name": r["name"],
            "code": r["ts_code"][:6],
            "run_date": f"{r['run_date'][:4]}-{r['run_date'][4:6]}-{r['run_date'][6:]}",
            "market_label": r["market_label"],
            "stop_loss": r.get("stop_loss") or 0.0,
            "pct_return": r.get("t1_return"),
            "hit_stop_loss": bool(r.get("t1_stop")),
        })
    return result


def _offset_date(date_str: str, days: int) -> str:
    """粗略日期偏移（仅用于过滤，不需精确到交易日）。"""
    from datetime import timedelta
    dt = datetime.strptime(date_str, "%Y%m%d") + timedelta(days=days)
    return dt.strftime("%Y%m%d")
