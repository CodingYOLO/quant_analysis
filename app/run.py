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

    # 推送：邮件发完整选股报告，标题突出"盘后选股"+候选数
    if not no_notify and final_state.report_md:
        notifier = get_notifier()
        n_cand = len(final_state.candidates)
        regime_label = getattr(final_state.market_regime, "label", "") or ""
        md, dd = trade_date[4:6], trade_date[6:]
        title = f"【盘后选股】{md}/{dd} {regime_label} | 候选{n_cand}只"
        # 邮件/微信均发完整报告全文（之前只发简报，用户要看完整版）
        ok = notifier.send(title, final_state.report_md)
        if ok:
            console.print("[green]📱 完整选股报告已推送（邮件全文+微信）[/green]")
        else:
            console.print("[yellow]⚠️  推送失败，请检查推送配置[/yellow]")


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


@cli.command("watch-scan")
@click.option("--force", is_flag=True, default=False, help="忽略交易时段限制（手动测试）")
@click.option("--no-push", is_flag=True, default=False, help="只算不推（预览）")
def watch_scan_cmd(force: bool, no_push: bool) -> None:
    """盯盘扫描：交易时段扫自选/持仓，命中触发(到买入价/破止损/异动)推 Bark。cron 每 2-3 分钟跑。"""
    from app.strategy.watch_alert import scan_watch_alerts
    alerts = scan_watch_alerts(push=not no_push, force=force)
    if alerts:
        console.print(f"[bold green]🛎️ 新触发 {len(alerts)} 条{'（已推 Bark）' if not no_push else ''}[/bold green]")
        for a in alerts:
            console.print(f"  {a['name']}: {'; '.join(a['triggers'])}")
    else:
        console.print("[dim]无新触发（或非交易时段，用 --force 强测）[/dim]")


@cli.command("market-scan")
@click.option("--force", is_flag=True, default=False, help="忽略交易时段限制（手动测试）")
@click.option("--no-push", is_flag=True, default=False, help="只算不推（预览）")
def market_scan_cmd(force: bool, no_push: bool) -> None:
    """全市场盘中提醒：扫雷达→板块弱转强/涨停潮/强势热点推 Bark（不必加自选）。cron 每15分钟跑。"""
    from app.strategy.market_alert import scan_market_alerts
    new = scan_market_alerts(push=not no_push, force=force)
    if new:
        console.print(f"[bold green]🌐 全市场新事件 {len(new)} 条{'（已推 Bark）' if not no_push else ''}[/bold green]")
        for a in new:
            console.print(f"  {a['title']}: {a['body'].splitlines()[0]}")
    else:
        console.print("[dim]无新市场事件（或非交易时段，用 --force 强测）[/dim]")


def _skip_non_trading_day(label: str) -> bool:
    """非交易日返回 True（调用方应跳过）。节假日撞工作日时防常规快讯空跑。"""
    try:
        from app.strategy.trade_calendar import is_trading_day
        if not is_trading_day():
            console.print(f"[yellow]⏸ 非交易日，跳过{label}（消息面报告另出）[/yellow]")
            return True
    except Exception:
        pass
    return False


@cli.command("pre")
@click.option("--date", "trade_date", default="", help="交易日 YYYYMMDD，默认今日")
@click.option("--no-notify", is_flag=True, default=False)
def run_pre(trade_date: str, no_notify: bool) -> None:
    """生成盘前快讯（隔夜消息+今日方向）。"""
    if not trade_date and _skip_non_trading_day("盘前快讯"):
        return
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
    if not trade_date and _skip_non_trading_day("盘中快讯"):
        return
    from app.nodes.quick_report import build_quick_report
    td = trade_date or None
    filepath, title, content = build_quick_report("mid", td)
    console.print(f"\n[bold green]✅ 盘中快讯已生成[/bold green]  {filepath}\n")
    if not no_notify:
        _push_quick_report(title, content)


@cli.command("post-quick")
@click.option("--date", "trade_date", default="", help="交易日 YYYYMMDD，默认今日")
@click.option("--no-notify", is_flag=True, default=False)
@click.option("--full", is_flag=True, default=False,
              help="完整版：标题标注'完整版'，用于晚间资金数据入库后的二次推送")
def run_post_quick(trade_date: str, no_notify: bool, full: bool) -> None:
    """生成盘后复盘快讯（全天新闻驱动复盘+明日预判）。"""
    if not trade_date and _skip_non_trading_day("盘后快讯"):
        return
    from app.nodes.quick_report import build_quick_report
    td = trade_date or None
    label_suffix = "完整版" if full else "速报"
    filepath, title, content = build_quick_report("post", td, label_suffix=label_suffix)
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


@cli.command("news-digest")
@click.option("--mode", default="daily", type=click.Choice(["daily", "preview"]),
              help="daily=消息面复盘+前瞻；preview=下周前瞻")
@click.option("--force", is_flag=True, default=False, help="忽略交易日判定，强制生成")
@click.option("--no-notify", is_flag=True, default=False)
def run_news_digest(mode: str, force: bool, no_notify: bool) -> None:
    """非交易日消息面报告（仅非交易日生成；研报/新闻/机会/风险）。"""
    if not force:
        from app.strategy.trade_calendar import is_last_nontrading_before_open, is_trading_day
        if is_trading_day():
            console.print("[yellow]⏸ 今日为交易日，跳过消息面报告[/yellow]")
            return
        if mode == "preview" and not is_last_nontrading_before_open():
            console.print("[yellow]⏸ 非'重开前最后一晚'，跳过下周前瞻[/yellow]")
            return
    from app.nodes.quick_report import build_news_digest
    filepath, title, content = build_news_digest(mode)
    console.print(f"\n[bold green]✅ {title} 已生成[/bold green]  {filepath}\n")
    if not no_notify:
        _push_quick_report(title, content)


@cli.command("pool-check")
@click.option("--force", is_flag=True, default=False, help="忽略交易日判定，强制运行")
@click.option("--no-notify", is_flag=True, default=False, help="不推送 Bark")
def run_pool_check_cmd(force: bool, no_notify: bool) -> None:
    """盘前·选股池消息面体检（交易日开盘前·对现有池逐只扫隔夜/周末新公告→利好/利空+博查舆情）。

    技术选股结果周末不变；本命令只刷新消息面：减持/解禁/业绩预告/快报/大宗/回购 + 舆情。
    周一一跑自然覆盖整个周末。
    """
    if not force:
        from app.strategy.trade_calendar import is_trading_day
        if not is_trading_day():
            console.print("[yellow]⏸ 今日非交易日，跳过选股池盘前体检[/yellow]")
            return
    from app.strategy.pool_premarket import run_pool_check
    res = run_pool_check(push=not no_notify)
    if res:
        console.print(f"\n[bold green]✅ {res[1]} 已生成[/bold green]  {res[0]}\n")
    else:
        console.print("[yellow]⏸ 暂无选股池，跳过盘前体检[/yellow]")


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


@cli.command("theme-llm")
@click.option("--date", "trade_date", default="last", help="交易日，默认最近交易日")
@click.option("--type", "theme_type", default="industry", help="industry / concept")
@click.option("--top", default=15, show_default=True, help="为热度前 N 的主题生成")
def theme_llm_cmd(trade_date: str, theme_type: str, top: int) -> None:
    """为热门主题批量生成接地式 LLM 解读 + 市场环境，落库（供 LLM 分析页读取）。"""
    from app.strategy.theme_llm import generate_for_date

    td = _resolve_date(trade_date)
    console.print(f"\n[bold cyan]🧠 生成主题 LLM 解读[/bold cyan]  {td} {theme_type} Top{top}\n")
    res = generate_for_date(td, theme_type, top)
    console.print(f"[green]✅ 生成 {res['generated']} 条主题解读 + 市场环境={res['env']}[/green]\n")


@cli.command("bull-catalysts")
@click.option("--date", "trade_date", default="last", help="交易日，默认最近交易日")
def bull_catalysts_cmd(trade_date: str) -> None:
    """🐂 牛股发掘·催化层：联网检索政策/新闻→LLM 映射到库内概念，落缓存（全部+科技两版·盘后 cron 预生成→前端秒开）。"""
    from app.strategy.bull_hunter import discover_catalysts

    td = _resolve_date(trade_date)
    console.print(f"\n[bold cyan]🐂 牛股发掘·政策催化扫描[/bold cyan]  {td}\n")
    for tech in (False, True):
        res = discover_catalysts(td, force=True, tech_only=tech)
        tag = "科技赛道" if tech else "全部"
        n = len(res.get("catalysts", []))
        color = "green" if res.get("ok") else "yellow"
        console.print(f"[{color}]  · {tag}：{n} 条催化{('' if res.get('ok') else ' — ' + res.get('msg', ''))}[/{color}]")
    console.print(f"[green]✅ 催化缓存已刷新 → data_cache/bull_catalyst/[/green]\n")


@cli.command("research-hub")
@click.option("--date", "trade_date", default="last", help="交易日，默认最近交易日")
def research_hub_cmd(trade_date: str) -> None:
    """📑 研报中心：博查抓券商研报观点→LLM 接地总结，落缓存（全部+科技两版·盘后 cron 预生成→前端秒开）。"""
    from app.strategy.bull_hunter import discover_research

    td = _resolve_date(trade_date)
    console.print(f"\n[bold cyan]📑 研报中心扫描[/bold cyan]  {td}\n")
    for tech in (False, True):
        res = discover_research(td, force=True, tech_only=tech)
        tag = "科技赛道" if tech else "全部"
        n = len(res.get("reports", []))
        color = "green" if res.get("ok") else "yellow"
        console.print(f"[{color}]  · {tag}：{n} 条研报{('' if res.get('ok') else ' — ' + res.get('msg', ''))}[/{color}]")
    console.print(f"[green]✅ 研报缓存已刷新 → data_cache/research_hub/[/green]\n")


@cli.command("stock-pool")
@click.option("--date", "trade_date", default="last", help="交易日，默认最近交易日")
@click.option("--no-reason", is_flag=True, default=False, help="跳过理由LLM生成(更快)")
@click.option("--skip-if-fresh", is_flag=True, default=False,
              help="若该交易日选股池已生成则直接跳过（兜底重跑用，避免重复花LLM费用）")
def stock_pool_cmd(trade_date: str, no_reason: bool, skip_if_fresh: bool) -> None:
    """运行内置策略选股池（5策略+多路置信度+风控），落库并生成理由（盘后cron）。"""
    from app.strategy.stock_pool import build_stock_pool, infer_market_label, generate_reasons
    from app.data.composite_provider import CompositeProvider
    from app.nodes.c_stock_selection import _data_ready

    td = _resolve_date(trade_date)
    # 兜底重跑：18:45 若已成功生成，则跳过，避免重复计算与重复 LLM 费用
    if skip_if_fresh:
        from app.strategy.db import pool_dates
        if td in pool_dates():
            console.print(f"[green]✅ {td} 选股池已存在，跳过（--skip-if-fresh）[/green]\n")
            return
    # 数据就绪校验——绝不用半截/旧数据生成选股池（资金流约17:15入库，cron 18:45 运行）
    ok, msg = _data_ready(td, CompositeProvider())
    if not ok:
        console.print(f"[red]⛔ {td} 数据未就绪，跳过选股池（不写库，保留上一交易日结果）：{msg}[/red]")
        console.print("[yellow]   稍后数据入库后重跑：python -m app.run stock-pool[/yellow]\n")
        return
    label = infer_market_label(td)
    console.print(f"\n[bold cyan]🎯 选股池[/bold cyan]  {td}  大盘={label}\n")
    pool = build_stock_pool(td, market_label=label, persist=True)
    focus = sum(1 for r in pool if r["is_focus"])
    console.print(f"[green]✅ 候选 {len(pool)} 只，最关注 {focus} 只 → strategy.db[/green]")
    if not no_reason:
        n = generate_reasons(td)
        console.print(f"[green]✅ 生成 {n} 条分析理由[/green]\n")


@cli.command("activity-rank")
@click.option("--days", default=1, show_default=True, help="回填最近N交易日（首次用20；日常cron用1-2）")
def activity_rank_cmd(days: int) -> None:
    """算全市场活跃度排名(换手+流通成交额)落 hot_rank_log，供人气反转选股（盘后cron·替代东财家用脚本）。"""
    from app.data.composite_provider import CompositeProvider
    from app.strategy.activity_rank import backfill_activity

    console.print(f"\n[bold cyan]🔥 活跃度排名[/bold cyan]  回填最近 {days} 交易日\n")
    r = backfill_activity(CompositeProvider(), int(days))
    console.print(f"[green]✅ {r['days_ok']}/{r['days_requested']} 日 · {r['rows']} 行 · 区间 {r['range']}[/green]\n")


@cli.command("wide")
@click.option("--date", "trade_date", default="last", show_default=True,
              help="交易日：YYYYMMDD / last（默认最近交易日）")
def build_wide_cmd(trade_date: str) -> None:
    """计算并落库 theme_heat_all_in_one 宽表（行业 + 同花顺概念，供 LLM 分析页读取）。"""
    from app.factors.theme_wide import (build_concept_wide, build_industry_l3_wide,
                                         build_industry_wide)

    td = _resolve_date(trade_date)
    console.print(f"\n[bold cyan]🧮 计算主题宽表[/bold cyan]  交易日 {td}（行业二级/三级 + 概念，首次约3-5分钟）\n")
    ind = build_industry_wide(td, persist=True)
    console.print(f"[green]✅ 行业(申万二级)：{len(ind)} 个[/green]")
    l3 = build_industry_l3_wide(td, persist=True)
    console.print(f"[green]✅ 细分(申万三级·PCB/光纤光缆/封测…)：{len(l3)} 个[/green]")
    con = build_concept_wide(td, persist=True)
    console.print(f"[green]✅ 概念：{len(con)} 个 → data_cache/theme_heat.db[/green]")
    # 预算各板块广度时序（复用全市场面板）→ 前端切板块秒开
    from app.factors.board_breadth import precompute_board_breadth
    nb = precompute_board_breadth(td)
    console.print(f"[green]✅ 板块广度预算：{nb} 个 → data_cache/board_breadth/[/green]\n")


@cli.command("pool-eval")
@click.option("--lookback", default=145, show_default=True, help="面板回看交易日")
@click.option("--step", default=3, show_default=True, help="采样步长(每N个交易日测一次)")
def pool_eval_cmd(lookback: int, step: int) -> None:
    """评分回测(A·历史价格结构)：对过去采样日按重点分分档统计 T+5 胜率，落库 pool_eval。"""
    from app.backtest.pool_eval import run_historical, aggregate
    from app.strategy.db import save_evals

    console.print(f"\n[bold cyan]📊 评分回测(历史·价格结构)[/bold cyan] 回看{lookback}日·步长{step}\n")
    evals = run_historical(lookback=lookback, step=step)
    n = save_evals(evals)
    agg = aggregate(evals, "强", "弱")
    console.print(f"[green]✅ 回测 {len(evals)} 个交易日 / 落库 {n} 行[/green]")
    if agg.get("n_days"):
        console.print(f"[green]总览：强档T+5胜率 {agg['strong_win']}% vs 弱档 {agg['weak_win']}% "
                      f"(差 {agg['spread']}pt·强>弱占 {agg['beat_ratio']}% 的天)[/green]\n")


@cli.command("signal-eval")
@click.option("--start", default="", help="回测开始日 YYYYMMDD（默认：结束日前30个自然日）")
@click.option("--end", default="", help="回测结束日 YYYYMMDD（默认：最近交易日）")
def signal_eval_cmd(start: str, end: str) -> None:
    """回测龙虎榜/炸板率信号的真实前向收益（纯量化统计，输出带时间戳报告）。"""
    from datetime import timedelta
    from app.backtest.signal_eval import build_signal_report

    end_date = end or _get_last_trade_date()
    if not start:
        start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")

    console.print(f"\n[bold cyan]🔬 信号回测启动[/bold cyan]  {start} ~ {end_date}（龙虎榜/炸板率）\n")
    path = build_signal_report(start, end_date)
    console.print(f"\n[bold green]✅ 信号回测报告已生成[/bold green]  {path}\n")


@cli.command("bocha-check")
def bocha_check() -> None:
    """验证博查 Bocha 联网搜索 API（填入 BOCHA_API_KEY 后跑一次真实检索）。"""
    from app.data.web_search import BochaSearchClient

    console.print("\n[bold cyan]🔍 博查 Bocha 联网搜索验证[/bold cyan]\n")
    health = BochaSearchClient().verify_connection()
    if health["ok"]:
        s = health["sample"]
        console.print(f"   状态: [green]✅ 连接正常（{health['detail']}）[/green]")
        console.print(f"   样例标题: {s['title']}")
        console.print(f"   来源/时间: {s['site']} {s['date']}")
        console.print(f"   摘要: {(s['summary'] or s['snippet'])[:80]}…")
        console.print(f"   URL: {s['url']}\n")
    else:
        console.print(f"   状态: [yellow]⚠️ {health['detail']}[/yellow]")
        console.print("   请在 .env 设置 BOCHA_API_KEY（https://open.bochaai.com 注册获取）\n")


@cli.command("cls-check")
def cls_check() -> None:
    """检查财联社 Cookie 是否仍然有效（到期前手动核验，约 2026-08 到期）。"""
    from app.data.composite_provider import CompositeProvider

    console.print("\n[bold cyan]🔍 财联社 Cookie 健康检查[/bold cyan]\n")
    health = CompositeProvider().check_cls_health()

    status_hint = {
        "ok":            ("green",  "✅ Cookie 有效"),
        "no_cookie":     ("yellow", "⚠️ 未配置 CLS_COOKIE（新闻将仅用东方财富）"),
        "expired":       ("red",    "❌ Cookie 已失效，请更新"),
        "empty":         ("yellow", "⚠️ 鉴权通过但无数据（接口异常，非 Cookie 问题）"),
        "network_error": ("yellow", "⚠️ 网络错误，无法判定（请重试）"),
    }
    color, label = status_hint.get(health["status"], ("white", health["status"]))
    console.print(f"   状态: [{color}]{label}[/{color}]")
    console.print(f"   详情: {health['detail']}")
    if health["ok"]:
        console.print(f"   本次取到电报: {health['count']} 条\n")
    elif health["status"] == "expired":
        console.print(
            "\n   [bold]更新步骤[/bold]：163 邮箱登录 → 打开 cls.cn 并登录 → "
            "F12 复制请求 Cookie → 填入 .env 的 CLS_COOKIE → 重启 astock-web\n"
        )
    else:
        console.print("")


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


@cli.command("warmup")
@click.option("--date", "base_date", default="", help="基准交易日 YYYYMMDD，默认今日")
def run_warmup(base_date: str) -> None:
    """预热重缓存：全市场因子表 + 大盘情绪仪表盘(默认近30天·含今/明/后覆盖周末)。
    供收盘后定时跑，用户晚上打开选股/大盘情绪/持仓页直接秒显示(磁盘缓存·重启不丢)。"""
    import datetime
    import time

    from app.data.composite_provider import CompositeProvider
    from app.strategy.market_sentiment import _latest_data_date, build_dashboard
    from app.strategy.screener import build_factor_table
    prov = CompositeProvider()
    base = base_date or datetime.date.today().strftime("%Y%m%d")
    t0 = time.time()
    # 1) 因子表（选股/板块强弱榜/持仓板块/超跌低吸都靠它·最重）
    latest = _latest_data_date(prov, base)
    try:
        build_factor_table(latest, prov)
        console.print(f"[green]✅ 因子表预热 {latest}[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠️ 因子表预热失败: {e}[/yellow]")
    # 1.5) 行业资金持续流入榜(近10日累计·按日缓存·晚上打开秒显示)
    try:
        from app.strategy.industry_flow import build_industry_persistent_flow
        build_industry_persistent_flow(latest, window=10)
        console.print(f"[green]✅ 行业持续流入榜预热 {latest}[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠️ 持续流入榜预热失败: {e}[/yellow]")
    # 1.55) 概念资金持续流入榜(渗透率+多窗口·宽成分周缓存首建~30s·避免用户次周首访等待)
    try:
        from app.strategy.concept_flow import build_concept_persistent_flow
        build_concept_persistent_flow(latest, window=10, provider=prov)
        console.print(f"[green]✅ 概念持续流入榜预热 {latest}[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠️ 概念持续流入榜预热失败: {e}[/yellow]")
    # 1.6) 板块诊断面板(状态机+回测分层+大类资金地图·按日缓存·打开秒显示)
    try:
        from app.strategy.sector_diagnosis import build_diagnosis
        build_diagnosis(latest, provider=prov, level="L2", force=True)
        console.print(f"[green]✅ 板块诊断预热 {latest}[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠️ 板块诊断预热失败: {e}[/yellow]")
    # 2) 大盘情绪默认区间(end=今/明/后·各往前30天·覆盖今晚与周末打开的缓存键)
    for off in (0, 1, 2):
        end = (datetime.datetime.strptime(base, "%Y%m%d") + datetime.timedelta(days=off)).strftime("%Y%m%d")
        start = (datetime.datetime.strptime(end, "%Y%m%d") - datetime.timedelta(days=30)).strftime("%Y%m%d")
        try:
            build_dashboard(end, start_date=start)
            console.print(f"[green]✅ 情绪预热 {start}~{end}[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠️ 情绪预热 {end} 失败: {e}[/yellow]")
    console.print(f"[bold green]🔥 预热完成 · 用时 {time.time() - t0:.0f}秒[/bold green]")


if __name__ == "__main__":
    # 支持: python -m app.run --date 20250613
    import sys
    if len(sys.argv) > 1 and sys.argv[1].startswith("--date"):
        # 直接调用 run 子命令
        sys.argv.insert(1, "run")
    cli()
