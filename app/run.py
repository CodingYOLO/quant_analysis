"""
CLI 入口。
用法:
  python -m app.run --date 20250613
  python -m app.run --date today
  python -m app.run --verify-only  # 只运行数据接口验证
"""

import logging
import time
from datetime import date, datetime

import click
from rich.console import Console

from app.config import get_settings
from app.graph import build_graph
from app.llm.client import LLMClient
from app.notify.notifier import get_notifier
from app.state import PipelineState

console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _resolve_date(date_str: str) -> str:
    """解析日期参数，支持 'today' / 'yesterday' / 'YYYYMMDD'。"""
    if date_str.lower() == "today":
        return date.today().strftime("%Y%m%d")
    if date_str.lower() == "yesterday":
        from datetime import timedelta
        return (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    # 校验格式
    datetime.strptime(date_str, "%Y%m%d")
    return date_str


@click.group()
def cli() -> None:
    """A股多Agent选股系统 CLI。"""


@cli.command("run")
@click.option("--date", "trade_date", default="yesterday", show_default=True,
              help="运行日期，格式 YYYYMMDD 或 today/yesterday")
@click.option("--no-notify", is_flag=True, default=False, help="跳过推送，只生成本地报告")
def run_pipeline(trade_date: str, no_notify: bool) -> None:
    """运行完整的选股流水线。"""
    settings = get_settings()
    trade_date = _resolve_date(trade_date)

    console.print(f"\n[bold cyan]🚀 A股选股系统启动[/bold cyan]")
    console.print(f"   交易日: {trade_date}")
    console.print(f"   推送渠道: {'禁用' if no_notify else settings.notify_channel}\n")

    # 初始化状态
    initial_state = PipelineState(trade_date=trade_date)

    # 构建图
    graph = build_graph()

    # 运行
    start = time.monotonic()
    try:
        final_state_dict = graph.invoke(initial_state.model_dump())
        final_state = PipelineState(**final_state_dict)
    except Exception as e:
        logger.error("流水线运行失败: %s", e, exc_info=True)
        raise

    elapsed = time.monotonic() - start

    # 查询 LLM 费用
    llm_client = LLMClient()
    cost_summary = llm_client.get_daily_cost_summary()

    console.print(f"\n[bold green]✅ 流水线完成[/bold green]")
    console.print(f"   耗时: {elapsed:.1f}s")
    console.print(f"   LLM调用: {cost_summary['calls']} 次")
    console.print(f"   Token消耗: {cost_summary['input_tokens']:,} in / {cost_summary['output_tokens']:,} out")
    console.print(f"   预估费用: ¥{cost_summary['estimated_cost_cny']:.4f}")
    console.print(f"   报告路径: {settings.report_dir}/{trade_date}.md\n")

    # 推送
    if not no_notify and final_state.report_md:
        notifier = get_notifier()
        title = f"A股简报 {trade_date} | {final_state.market_regime.label}"
        ok = notifier.send(title, final_state.report_md)
        if ok:
            console.print("[green]📱 微信推送成功[/green]")
        else:
            console.print("[yellow]⚠️  推送失败，请检查 SendKey 配置[/yellow]")


@cli.command("verify")
def verify_data() -> None:
    """验证所有数据接口是否可用（真实拉取并打印字段）。"""
    from app.data.verify import run_all_verifications
    run_all_verifications()


@cli.command("cost")
def show_cost() -> None:
    """查看今日 LLM 调用费用汇总。"""
    llm_client = LLMClient()
    summary = llm_client.get_daily_cost_summary()
    console.print(f"\n📊 今日 LLM 费用汇总 ({summary['date']})")
    console.print(f"   调用次数: {summary['calls']}")
    console.print(f"   输入Token: {summary['input_tokens']:,}")
    console.print(f"   输出Token: {summary['output_tokens']:,}")
    console.print(f"   预估费用: ¥{summary['estimated_cost_cny']:.4f}\n")


# 支持 python -m app.run --date xxx（旧式入口，兼容文档示例）
@click.command("main")
@click.option("--date", "trade_date", default="yesterday")
def _legacy_main(trade_date: str) -> None:
    """兼容 python -m app.run --date YYYYMMDD 的调用方式。"""
    from click.testing import CliRunner
    runner = CliRunner()
    runner.invoke(run_pipeline, ["--date", trade_date])


if __name__ == "__main__":
    # 支持: python -m app.run --date 20250613
    import sys
    if len(sys.argv) > 1 and sys.argv[1].startswith("--date"):
        # 直接调用 run 子命令
        sys.argv.insert(1, "run")
    cli()
