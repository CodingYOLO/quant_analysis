"""
回测引擎：给定历史区间，对每个交易日跑选股流水线，
统计候选池在未来 N 日的收益分布（胜率/盈亏比/最大回撤）。

用法:
  python -m app.backtest.engine --start 20250101 --end 20250613 --hold-days 3
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix
from app.nodes.c_stock_selection import _run_selection_pipeline
from app.nodes.a_market_gate import _calc_market_regime
from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """回测统计结果。"""
    start_date: str
    end_date: str
    hold_days: int
    total_trades: int = 0           # 总入场次数
    win_trades: int = 0             # 盈利次数（收益>0）
    win_rate: float = 0.0           # 胜率
    avg_return: float = 0.0         # 平均收益率
    avg_win: float = 0.0            # 盈利时平均收益
    avg_loss: float = 0.0           # 亏损时平均损失
    profit_loss_ratio: float = 0.0  # 盈亏比 = avg_win / |avg_loss|
    max_drawdown: float = 0.0       # 单笔最大亏损
    returns: list[float] = field(default_factory=list)  # 每笔收益率

    def summary(self) -> str:
        return (
            f"回测区间: {self.start_date} ~ {self.end_date}  持仓: {self.hold_days}日\n"
            f"总交易次数: {self.total_trades}\n"
            f"胜率:       {self.win_rate:.1%}\n"
            f"平均收益:   {self.avg_return:.2%}\n"
            f"盈利均值:   {self.avg_win:.2%}\n"
            f"亏损均值:   {self.avg_loss:.2%}\n"
            f"盈亏比:     {self.profit_loss_ratio:.2f}\n"
            f"最大单笔亏: {self.max_drawdown:.2%}\n"
        )


def run_backtest(
    start_date: str,
    end_date: str,
    hold_days: int = 3,
) -> BacktestResult:
    """
    对历史区间内每个交易日运行选股流水线，计算候选股未来 hold_days 日收益。

    注意：
    - 每日用当日及之前数据选股（无未来数据泄漏）
    - 买入价 = 次一交易日开盘价（T+1）
    - 卖出价 = 买入后第 hold_days 个交易日的收盘价
    """
    provider = CompositeProvider()
    settings = get_settings()
    result = BacktestResult(start_date=start_date, end_date=end_date, hold_days=hold_days)

    # 获取区间内所有交易日
    cal = provider.get_trade_cal(start_date, end_date)
    trade_dates = sorted(cal[cal["is_open"] == 1]["cal_date"].tolist())

    # 需要额外拉 hold_days+1 天的后续数据用于计算收益
    # 这里简化处理：先把所有价格矩阵加载到最新日期
    logger.info("回测区间 %s ~ %s，共 %d 个交易日", start_date, end_date, len(trade_dates))

    all_returns = []

    for i, trade_date in enumerate(trade_dates):
        # 确保有足够的后续数据
        future_dates = trade_dates[i + 1 : i + hold_days + 2]
        if len(future_dates) < hold_days + 1:
            logger.debug("跳过 %s：后续交易日不足", trade_date)
            continue

        try:
            # 市场择时判断
            regime = _calc_market_regime(trade_date, provider)
            if not regime.can_open:
                logger.debug("%s 市场状态=%s，跳过", trade_date, regime.label)
                continue

            # 选股
            candidates = _run_selection_pipeline(
                trade_date=trade_date,
                provider=provider,
                max_candidates=settings.max_candidates,
                min_market_cap=settings.min_market_cap,
                max_market_cap=settings.max_market_cap,
            )

            if not candidates:
                continue

            # 计算每只候选股的 hold_days 日收益
            buy_date = future_dates[0]       # T+1 开盘买入
            sell_date = future_dates[hold_days]  # T+hold_days 收盘卖出

            buy_daily = provider.get_daily(buy_date)
            sell_daily = provider.get_daily(sell_date)

            if buy_daily is None or sell_daily is None:
                continue

            buy_prices = buy_daily.set_index("ts_code")["open"]
            sell_prices = sell_daily.set_index("ts_code")["close"]

            for c in candidates:
                code = c.code
                if code not in buy_prices.index or code not in sell_prices.index:
                    continue
                buy_p = buy_prices[code]
                sell_p = sell_prices[code]
                if buy_p <= 0:
                    continue
                ret = (sell_p - buy_p) / buy_p
                all_returns.append(ret)

            logger.info("%s 选出 %d 只，有效收益 %d 笔", trade_date, len(candidates), len(all_returns))

        except Exception as e:
            logger.warning("回测 %s 出错: %s", trade_date, e)
            continue

    # 统计
    return _calc_stats(result, all_returns)


def _calc_stats(result: BacktestResult, returns: list[float]) -> BacktestResult:
    """计算统计指标。"""
    if not returns:
        logger.warning("回测无有效交易记录")
        return result

    result.returns = returns
    result.total_trades = len(returns)

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    result.win_trades = len(wins)
    result.win_rate = len(wins) / len(returns)
    result.avg_return = sum(returns) / len(returns)
    result.avg_win = sum(wins) / len(wins) if wins else 0.0
    result.avg_loss = sum(losses) / len(losses) if losses else 0.0
    result.profit_loss_ratio = abs(result.avg_win / result.avg_loss) if result.avg_loss != 0 else float("inf")
    result.max_drawdown = min(returns)

    return result


if __name__ == "__main__":
    import click

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    @click.command()
    @click.option("--start", default="20250101", help="回测开始日期 YYYYMMDD")
    @click.option("--end", default="20250613", help="回测结束日期 YYYYMMDD")
    @click.option("--hold-days", default=3, help="持仓天数")
    def main(start: str, end: str, hold_days: int) -> None:
        result = run_backtest(start, end, hold_days)
        print("\n" + "=" * 50)
        print(result.summary())

        # 保存收益分布到 CSV
        if result.returns:
            df = pd.DataFrame({"return": result.returns})
            path = f"reports/backtest_{start}_{end}_hold{hold_days}.csv"
            df.to_csv(path, index=False)
            print(f"收益分布已保存: {path}")

    main()
