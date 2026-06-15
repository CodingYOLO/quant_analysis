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
    """
    解析日期参数。
    - 'last'（默认）：自动从交易日历找最近的交易日，避免周末/节假日误用
    - 'today' / 'yesterday'：直接转换，不校验是否为交易日
    - 'YYYYMMDD'：指定日期
    """
    if date_str.lower() in ("last", "latest"):
        return _get_last_trade_date()
    if date_str.lower() == "today":
        return date.today().strftime("%Y%m%d")
    if date_str.lower() == "yesterday":
        from datetime import timedelta
        return (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    # 校验格式
    datetime.strptime(date_str, "%Y%m%d")
    return date_str


def _get_prev_trade_date(trade_date: str) -> str | None:
    """
    获取 trade_date 的前一个交易日（用于 O13 回填）。
    从 Tushare 交易日历查询，找到 trade_date 之前最近的开市日。
    """
    from datetime import datetime, timedelta
    from app.data.tushare_provider import TushareProvider

    try:
        dt = datetime.strptime(trade_date, "%Y%m%d")
        start = (dt - timedelta(days=10)).strftime("%Y%m%d")
        end = (dt - timedelta(days=1)).strftime("%Y%m%d")

        provider = TushareProvider()
        cal = provider.get_trade_cal(start, end)
        open_days = (
            cal[cal["is_open"] == 1]["cal_date"]
            .sort_values(ascending=False)
            .tolist()
        )
        return open_days[0] if open_days else None
    except Exception as e:
        logger.debug("获取前一交易日失败: %s", e)
        return None


def _backfill_forward_tracking(trade_date: str) -> None:
    """
    pipeline 启动时，用今日及历史价格回填所有未完成的前向追踪记录。
    首次运行无记录时静默跳过，不影响主流程。
    """
    try:
        from datetime import timedelta
        from app.data.composite_provider import CompositeProvider
        from app.data.tushare_provider import TushareProvider
        from app.strategy.forward_tracker import backfill_forward

        # 获取最近 30 个交易日列表（足够覆盖 T+5 回填所需的历史）
        dt_str = trade_date
        from datetime import datetime
        start = (datetime.strptime(dt_str, "%Y%m%d") - timedelta(days=45)).strftime("%Y%m%d")
        ts = TushareProvider()
        cal = ts.get_trade_cal(start, dt_str)
        all_dates: list[str] = sorted(
            cal[cal["is_open"] == 1]["cal_date"].tolist()
        )

        provider = CompositeProvider()
        backfill_forward(trade_date, provider, all_dates)
    except Exception as e:
        logger.debug("[前向追踪] 回填跳过（非关键）: %s", e)


def _get_last_trade_date() -> str:
    """
    从 Tushare 交易日历获取距今最近的交易日（含今日）。
    周末/节假日运行时会自动回退到上一个交易日。
    """
    from datetime import timedelta
    from app.data.tushare_provider import TushareProvider

    today = date.today()
    # 往前查7天，足够覆盖节假日连休
    start = (today - timedelta(days=7)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    try:
        provider = TushareProvider()
        cal = provider.get_trade_cal(start, end)
        open_days = (
            cal[cal["is_open"] == 1]["cal_date"]
            .sort_values(ascending=False)
            .tolist()
        )
        if open_days:
            last_trade = open_days[0]
            if last_trade != end:
                console.print(
                    f"   [yellow]今日({end})为非交易日，自动使用最近交易日 {last_trade}[/yellow]"
                )
            return last_trade
    except Exception as e:
        logger.warning("获取交易日历失败，回退到昨日: %s", e)

    # 兜底：回退到昨日
    from datetime import timedelta
    return (today - timedelta(days=1)).strftime("%Y%m%d")


@click.group()
def cli() -> None:
    """A股多Agent选股系统 CLI。"""


@cli.command("run")
@click.option("--date", "trade_date", default="last", show_default=True,
              help="运行日期：YYYYMMDD / today / yesterday / last（默认：自动取最近交易日）")
@click.option("--no-notify", is_flag=True, default=False, help="跳过推送，只生成本地报告")
def run_pipeline(trade_date: str, no_notify: bool) -> None:
    """运行完整的选股流水线。"""
    settings = get_settings()
    trade_date = _resolve_date(trade_date)

    console.print(f"\n[bold cyan]🚀 A股选股系统启动[/bold cyan]")
    console.print(f"   交易日: {trade_date}")
    console.print(f"   推送渠道: {'禁用' if no_notify else settings.notify_channel}\n")

    # 前向追踪回填（用今日及历史价格，回填未完成的 T+1/T+3/T+5）
    _backfill_forward_tracking(trade_date)

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

    # 将耗时和费用写回报告（流水线结束后二次写入）
    final_state.meta.elapsed_seconds = round(elapsed, 1)
    final_state.meta.total_tokens = cost_summary["input_tokens"] + cost_summary["output_tokens"]
    final_state.meta.estimated_cost_cny = cost_summary["estimated_cost_cny"]
    _rewrite_run_info(settings, trade_date, final_state)

    console.print(f"\n[bold green]✅ 流水线完成[/bold green]")
    console.print(f"   耗时: {elapsed:.1f}s")
    console.print(f"   LLM调用: {cost_summary['calls']} 次")
    console.print(f"   Token消耗: {cost_summary['input_tokens']:,} in / {cost_summary['output_tokens']:,} out")
    console.print(f"   预估费用: ¥{cost_summary['estimated_cost_cny']:.4f}")
    console.print(f"   报告路径: {settings.report_dir}/{trade_date}.md\n")

    # 推送：手机收简报，Web查完整报告
    if not no_notify and final_state.report_md:
        notifier = get_notifier()
        from app.notify.summary import build_post_market_summary
        web_url = settings.web_base_url if hasattr(settings, "web_base_url") else ""
        title, summary = build_post_market_summary(final_state, web_url=web_url)
        ok = notifier.send(title, summary)
        if ok:
            console.print("[green]📱 微信简报推送成功[/green]")
        else:
            console.print("[yellow]⚠️  推送失败，请检查 SendKey 配置[/yellow]")


@cli.command("backtest")
@click.option("--start", default="20260101", show_default=True, help="回测开始日 YYYYMMDD")
@click.option("--end", default="", help="回测结束日（默认：最近交易日）")
@click.option("--use-llm", is_flag=True, default=False, help="启用LLM多空辩论过滤（慢，接近实盘）")
def run_backtest_cmd(start: str, end: str, use_llm: bool) -> None:
    """运行历史回测并将逐笔结果存入 strategy.db，再输出策略分析报告。"""
    from app.backtest.engine import run_backtest
    from app.strategy.analyzer import full_analysis, print_analysis

    end_date = end or _get_last_trade_date()
    console.print(f"\n[bold cyan]📈 回测启动[/bold cyan]  {start} ~ {end_date}  LLM={'开' if use_llm else '关（纯量化）'}\n")

    result = run_backtest(start, end_date, use_llm=use_llm, save_to_db=True)
    console.print(result.summary())

    console.print("\n[bold cyan]🔬 策略因子分析[/bold cyan]\n")
    analysis = full_analysis(is_backtest=1, start_date=start, end_date=end_date)
    print_analysis(analysis)


@cli.command("pre")
@click.option("--date", "trade_date", default="", help="交易日 YYYYMMDD，默认今日")
@click.option("--no-notify", is_flag=True, default=False)
def run_pre(trade_date: str, no_notify: bool) -> None:
    """生成盘前快讯（隔夜消息+今日方向）。"""
    from app.nodes.quick_report import build_quick_report
    td = trade_date or None
    filepath, title, content = build_quick_report("pre", td)
    console.print(f"\n[bold green]✅ 盘前快讯已生成[/bold green]  {filepath}\n")
    if not no_notify:
        _push_quick_report(title, content)


@cli.command("mid")
@click.option("--date", "trade_date", default="", help="交易日 YYYYMMDD，默认今日")
@click.option("--no-notify", is_flag=True, default=False)
def run_mid(trade_date: str, no_notify: bool) -> None:
    """生成盘中半天快讯（上午催化+午后策略）。"""
    from app.nodes.quick_report import build_quick_report
    td = trade_date or None
    filepath, title, content = build_quick_report("mid", td)
    console.print(f"\n[bold green]✅ 盘中快讯已生成[/bold green]  {filepath}\n")
    if not no_notify:
        _push_quick_report(title, content)


@cli.command("post-quick")
@click.option("--date", "trade_date", default="", help="交易日 YYYYMMDD，默认今日")
@click.option("--no-notify", is_flag=True, default=False)
def run_post_quick(trade_date: str, no_notify: bool) -> None:
    """生成盘后复盘快讯（全天新闻驱动复盘+明日预判）。"""
    from app.nodes.quick_report import build_quick_report
    td = trade_date or None
    filepath, title, content = build_quick_report("post", td)
    console.print(f"\n[bold green]✅ 盘后快讯已生成[/bold green]  {filepath}\n")
    if not no_notify:
        _push_quick_report(title, content)


def _push_quick_report(title: str, content: str) -> None:
    """推送快讯完整内容：邮件发 HTML 全文，Server酱发完整 Markdown。"""
    try:
        notifier = get_notifier()
        ok = notifier.send(title[:32], content)
        if ok:
            console.print("[green]📱 快讯已推送到手机[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠️ 推送失败: {e}[/yellow]")


@cli.command("web")
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True)
def run_web(host: str, port: int) -> None:
    """启动 Web UI（http://localhost:8000）。"""
    from app.web.main import start_server
    console.print(f"\n[bold cyan]🌐 Web UI 启动[/bold cyan]  http://localhost:{port}\n")
    start_server(host=host, port=port)


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


def _rewrite_run_info(settings, trade_date: str, state: "PipelineState") -> None:
    """流水线完成后，把真实耗时和费用写入已生成的报告末尾。"""
    # 数据缺失（非交易日）时不写报告
    if state.market_regime and state.market_regime.label == "数据缺失":
        return
    from app.nodes.e_report import _save_report
    from app.nodes.e_report import _build_report
    report = _build_report(state)
    _save_report(trade_date, report)


if __name__ == "__main__":
    # 支持: python -m app.run --date 20250613
    import sys
    if len(sys.argv) > 1 and sys.argv[1].startswith("--date"):
        # 直接调用 run 子命令
        sys.argv.insert(1, "run")
    cli()
