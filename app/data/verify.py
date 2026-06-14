"""
数据接口真实验证脚本。
每个 verify_*() 函数真实调用接口并打印 columns + 前3行。
运行: python -m app.data.verify
在上层逻辑使用任何接口前必须先跑通对应的 verify_*() 函数。
"""

import logging
import sys
from datetime import date, timedelta

import pandas as pd
from rich.console import Console
from rich.table import Table

console = Console()
logging.basicConfig(level=logging.WARNING)


def _print_df(name: str, df: pd.DataFrame | None) -> None:
    """打印接口返回的列名和前3行数据。"""
    if df is None or df.empty:
        console.print(f"[red]❌ {name}: 返回空数据[/red]")
        return

    console.print(f"\n[green]✅ {name}[/green]")
    console.print(f"   列名: {list(df.columns)}")
    console.print(f"   行数: {len(df)}")

    table = Table(show_header=True, header_style="bold cyan")
    for col in df.columns[:8]:  # 最多显示8列避免太宽
        table.add_column(str(col), max_width=15)
    for _, row in df.head(3).iterrows():
        table.add_row(*[str(row[c])[:15] for c in df.columns[:8]])
    console.print(table)


def _get_last_trade_date() -> str:
    """获取最近的交易日（简单回退到最近的工作日）。"""
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:  # 跳过周末
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


# ============================================================
# Tushare 验证
# ============================================================

def verify_tushare_daily(trade_date: str) -> bool:
    console.print(f"\n[bold]--- Tushare: daily ({trade_date}) ---[/bold]")
    try:
        from app.data.tushare_provider import TushareProvider
        p = TushareProvider()
        df = p.get_daily(trade_date)
        _print_df("tushare_daily", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ tushare_daily 异常: {e}[/red]")
        return False


def verify_tushare_stock_basic() -> bool:
    console.print("\n[bold]--- Tushare: stock_basic ---[/bold]")
    try:
        from app.data.tushare_provider import TushareProvider
        p = TushareProvider()
        df = p.get_stock_basic()
        _print_df("tushare_stock_basic", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ tushare_stock_basic 异常: {e}[/red]")
        return False


def verify_tushare_trade_cal() -> bool:
    console.print("\n[bold]--- Tushare: trade_cal ---[/bold]")
    try:
        from app.data.tushare_provider import TushareProvider
        p = TushareProvider()
        df = p.get_trade_cal("20250601", "20250630")
        _print_df("tushare_trade_cal", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ tushare_trade_cal 异常: {e}[/red]")
        return False


def verify_tushare_index_daily(trade_date: str) -> bool:
    console.print(f"\n[bold]--- Tushare: index_daily (沪深300, {trade_date}) ---[/bold]")
    try:
        from app.data.tushare_provider import TushareProvider
        p = TushareProvider()
        df = p.get_index_daily("399300.SZ", trade_date)
        _print_df("tushare_index_daily", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ tushare_index_daily 异常: {e}[/red]")
        return False


def verify_tushare_money_flow(trade_date: str) -> bool:
    console.print(f"\n[bold]--- Tushare: moneyflow ({trade_date}) ---[/bold]")
    try:
        from app.data.tushare_provider import TushareProvider
        p = TushareProvider()
        df = p.get_money_flow(trade_date)
        _print_df("tushare_moneyflow", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ tushare_moneyflow 异常: {e}[/red]")
        return False


def verify_tushare_lhb(trade_date: str) -> bool:
    console.print(f"\n[bold]--- Tushare: 龙虎榜 top_list ({trade_date}) ---[/bold]")
    try:
        from app.data.tushare_provider import TushareProvider
        p = TushareProvider()
        df = p.get_lhb_detail(trade_date)
        _print_df("tushare_lhb", df)
        return True  # 龙虎榜可能某天没有数据，空也算通过
    except Exception as e:
        console.print(f"[red]❌ tushare_lhb 异常: {e}[/red]")
        return False


def verify_tushare_north_flow(trade_date: str) -> bool:
    console.print(f"\n[bold]--- Tushare: 北向资金 moneyflow_hsgt ({trade_date}) ---[/bold]")
    try:
        from app.data.tushare_provider import TushareProvider
        p = TushareProvider()
        df = p.get_north_flow(trade_date)
        _print_df("tushare_north_flow", df)
        return True
    except Exception as e:
        console.print(f"[red]❌ tushare_north_flow 异常: {e}[/red]")
        return False


# ============================================================
# Akshare 验证
# ============================================================

def verify_akshare_spot_em() -> bool:
    console.print("\n[bold]--- Akshare: stock_zh_a_spot_em ---[/bold]")
    try:
        from app.data.akshare_provider import AkshareProvider
        p = AkshareProvider()
        df = p.get_spot_em()
        _print_df("ak_spot_em", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ ak_spot_em 异常: {e}[/red]")
        return False


def verify_akshare_concept_list() -> bool:
    console.print("\n[bold]--- Akshare: stock_board_concept_name_em ---[/bold]")
    try:
        from app.data.akshare_provider import AkshareProvider
        p = AkshareProvider()
        df = p.get_concept_list()
        _print_df("ak_concept_list", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ ak_concept_list 异常: {e}[/red]")
        return False


def verify_akshare_industry_list() -> bool:
    console.print("\n[bold]--- Akshare: stock_board_industry_name_em ---[/bold]")
    try:
        from app.data.akshare_provider import AkshareProvider
        p = AkshareProvider()
        df = p.get_industry_list()
        _print_df("ak_industry_list", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ ak_industry_list 异常: {e}[/red]")
        return False


def verify_akshare_sector_fund_flow() -> bool:
    console.print("\n[bold]--- Akshare: stock_sector_fund_flow_rank ---[/bold]")
    try:
        from app.data.akshare_provider import AkshareProvider
        p = AkshareProvider()
        df = p.get_sector_fund_flow()
        _print_df("ak_sector_fund_flow", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ ak_sector_fund_flow 异常: {e}[/red]")
        return False


def verify_akshare_stock_comment() -> bool:
    console.print("\n[bold]--- Akshare: stock_comment_em (千股千评) ---[/bold]")
    try:
        from app.data.akshare_provider import AkshareProvider
        p = AkshareProvider()
        # 用任意交易日，实际拉取的是最新数据
        df = p.get_stock_comment(date.today().strftime("%Y%m%d"))
        _print_df("ak_stock_comment", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ ak_stock_comment 异常: {e}[/red]")
        return False


def verify_akshare_cls_news() -> bool:
    console.print("\n[bold]--- Akshare: stock_info_global_cls (财联社电报) ---[/bold]")
    try:
        from app.data.akshare_provider import AkshareProvider
        p = AkshareProvider()
        df = p.get_cls_news(date.today().strftime("%Y%m%d"))
        _print_df("ak_cls_news", df)
        return not df.empty
    except Exception as e:
        console.print(f"[red]❌ ak_cls_news 异常: {e}[/red]")
        return False


def verify_akshare_stock_news() -> bool:
    console.print("\n[bold]--- Akshare: stock_news_em (个股新闻，以宁德时代300750为例) ---[/bold]")
    try:
        from app.data.akshare_provider import AkshareProvider
        p = AkshareProvider()
        df = p.get_stock_news("300750.SZ")
        _print_df("ak_stock_news", df)
        return True  # 可能没有新闻，空也允许
    except Exception as e:
        console.print(f"[red]❌ ak_stock_news 异常: {e}[/red]")
        return False


# ============================================================
# 汇总运行
# ============================================================

def run_all_verifications() -> None:
    """依次运行所有接口验证，最后打印汇总报告。"""
    trade_date = _get_last_trade_date()
    console.print(f"\n[bold yellow]🔍 开始验证所有数据接口（参考交易日: {trade_date}）[/bold yellow]\n")

    results: dict[str, bool] = {}

    # Tushare
    results["tushare_daily"] = verify_tushare_daily(trade_date)
    results["tushare_stock_basic"] = verify_tushare_stock_basic()
    results["tushare_trade_cal"] = verify_tushare_trade_cal()
    results["tushare_index_daily"] = verify_tushare_index_daily(trade_date)
    results["tushare_moneyflow"] = verify_tushare_money_flow(trade_date)
    results["tushare_lhb"] = verify_tushare_lhb(trade_date)
    results["tushare_north_flow"] = verify_tushare_north_flow(trade_date)

    # Akshare
    results["ak_spot_em"] = verify_akshare_spot_em()
    results["ak_concept_list"] = verify_akshare_concept_list()
    results["ak_industry_list"] = verify_akshare_industry_list()
    results["ak_sector_fund_flow"] = verify_akshare_sector_fund_flow()
    results["ak_stock_comment"] = verify_akshare_stock_comment()
    results["ak_cls_news"] = verify_akshare_cls_news()
    results["ak_stock_news"] = verify_akshare_stock_news()

    # 汇总
    console.print("\n" + "=" * 50)
    console.print("[bold]验证汇总[/bold]")
    passed = sum(results.values())
    total = len(results)
    for name, ok in results.items():
        icon = "✅" if ok else "❌"
        console.print(f"  {icon} {name}")
    console.print(f"\n通过: {passed}/{total}")

    if passed < total:
        console.print("[yellow]⚠️  部分接口验证失败，请检查上方错误信息后再运行上层逻辑。[/yellow]")
        sys.exit(1)
    else:
        console.print("[green]🎉 所有接口验证通过！[/green]")


if __name__ == "__main__":
    run_all_verifications()
