"""
节点 E: 报告生成与推送。
汇总 PipelineState → Markdown 报告（含止损止盈、次日观察清单）→ 推送。
"""

import logging
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.state import Candidate, PipelineState, SectorStat

logger = logging.getLogger(__name__)


def node_report(state: PipelineState) -> PipelineState:
    """生成 Markdown 报告并写入文件。"""
    logger.info("[节点E] 报告生成")
    report = _build_report(state)
    state.report_md = report
    _save_report(state.trade_date, report)
    return state


def _build_report(state: PipelineState) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    regime = state.market_regime
    meta = state.meta

    lines = [
        f"# A股每日选股简报 — {state.trade_date}",
        f"> 生成时间：{now}  |  定位：信息聚合+量化初筛，不构成投资建议",
        "",
    ]

    # ---- 第一部分：大盘择时 ----
    _append_market_section(lines, regime)

    # ---- 第二部分：板块热度（Phase 2 量化）----
    _append_sector_section(lines, state)

    # ---- 第三部分：候选股总览表 ----
    lines += ["", "## 三、候选股票池"]
    if state.candidates:
        _append_candidates_table(lines, state.candidates)
        lines.append("")
        lines.append(
            "> ⚠️ **免责**：量化初筛结果，需自行核查基本面"
            "（扣非净利润连续2季度为正、商誉<净资产30%、无大股东减持公告）。"
            "止损止盈为系统建议，不构成买入建议。"
        )

        # ---- 第四部分：逐股详情（走势+交易计划+次日清单）----
        lines += ["", "## 四、个股详情与执行计划"]
        for i, c in enumerate(state.candidates, 1):
            _append_stock_detail(lines, i, c, regime.label)
    else:
        lines.append("_（今日无候选股通过量化筛选，建议空仓观望）_")

    # ---- 元信息 ----
    lines += [
        "",
        "---",
        "## 运行信息",
        f"- 耗时：{meta.elapsed_seconds:.1f}s  Token消耗：{meta.total_tokens:,}  "
        f"预估费用：¥{meta.estimated_cost_cny:.4f}",
    ]
    if meta.errors:
        lines += ["", "**运行警告：**"]
        for e in meta.errors:
            lines.append(f"- {e}")

    return "\n".join(lines)


def _append_market_section(lines: list, regime) -> None:
    """写入大盘择时区块。"""
    state_emoji = {
        "主升": "🚀", "升温": "📈", "震荡": "➡️",
        "退潮反抽": "⚠️", "弱势": "📉", "衰退": "🔴",
    }.get(regime.label, "❓")

    lines += [
        "## 一、大盘择时",
        f"### {state_emoji} 市场状态：{regime.label}　|　"
        f"{'✅ 可开仓' if regime.can_open else '❌ 暂停开仓'}　|　"
        f"仓位上限 {regime.position_limit:.0%}",
        "",
        "| 指标 | 数值 | 信号 |",
        "|---|---|---|",
        f"| 涨停家数 | {regime.limit_up_count} 家 | "
        f"{'✅ 强' if regime.limit_up_count >= 80 else '⚠️ 弱'} |",
        f"| 跌停家数 | {regime.limit_down_count} 家 | "
        f"{'✅ 安全' if regime.limit_down_count < 15 else '❌ 危险'} |",
        f"| 全市场下跌家数 | {regime.down_count} 家 | "
        f"{'✅ 可控' if regime.down_count < 2000 else '⚠️ 偏多'} |",
        f"| 站上MA5占比 | {regime.pct_above_ma5:.1%} | "
        f"{'✅' if regime.pct_above_ma5 > 0.55 else '⚠️'} |",
        f"| 沪深300站MA5 | {'✅ 是' if regime.hs300_above_ma5 else '❌ 否'} | — |",
        f"| 沪深300站MA20 | {'✅ 是' if regime.hs300_above_ma20 else '❌ 否'} | — |",
        f"| 北向资金 | {regime.north_net_million:+.0f} 万元 | "
        f"{'✅ 净流入' if regime.north_net_million > 0 else '⚠️ 净流出'} |",
        "",
        f"> **判断依据**：{regime.reason}",
    ]


def _append_sector_section(lines: list, state: PipelineState) -> None:
    """写入板块热度与阶段区块（Phase 2 量化输出）。"""
    lines += ["", "## 二、板块热度与阶段"]

    sector_stats = state.sector_stats
    if not sector_stats:
        lines.append("_（板块热度数据获取失败或当日无数据）_")
        return

    buy_sectors = [s for s in sector_stats if s.decision == "buy"]
    watch_sectors = [s for s in sector_stats if s.decision == "watch"]
    avoid_sectors = [s for s in sector_stats if s.phase == "退潮"][:5]

    # ---- 决策总览 ----
    lines += [
        "",
        f"> 共分析 **{len(sector_stats)}** 个行业板块 | "
        f"🟢 可关注 **{len(buy_sectors)}** 个 | "
        f"🟡 观望 **{len(watch_sectors)}** 个 | "
        f"🔴 退潮预警 **{len(avoid_sectors)}** 个",
    ]

    # ---- 可关注板块（buy）----
    if buy_sectors:
        lines += [
            "",
            f"### 🟢 可关注板块（决策评分 ≥ 60，共 {len(buy_sectors)} 个）",
            "",
            "| 板块 | 决策分 | 热度 | 3日Δ | 阶段 | 5日资金(亿) | MA20广度 | 连板 | 人气集中 | 次日风险 | 信号 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for s in buy_sectors[:10]:
            delta_str = f"{s.heat_score_delta_3d:+.1f}" if s.heat_score_delta_3d != 0 else "—"
            lines.append(
                f"| {s.industry} | **{s.decision_score}** "
                f"| {s.heat_score:.0f} | {delta_str} "
                f"| {s.phase} "
                f"| {s.flow_5d_100m:+.1f} "
                f"| {s.pct_above_ma20:.0%} "
                f"| {s.consecutive_limit_high}板 "
                f"| {s.pop_concentration:.0%} "
                f"| {s.nextday_risk_penalty:.0f} "
                f"| {s.signal} |"
            )

    # ---- 观望板块（watch）----
    if watch_sectors:
        lines += [
            "",
            "### 🟡 观望板块（决策评分 40-59）",
            "",
            "| 板块 | 决策分 | 热度 | 3日Δ | 阶段 | 5日资金(亿) | 人气集中 | 次日风险 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for s in watch_sectors[:8]:
            delta_str = f"{s.heat_score_delta_3d:+.1f}" if s.heat_score_delta_3d != 0 else "—"
            lines.append(
                f"| {s.industry} | {s.decision_score} "
                f"| {s.heat_score:.0f} | {delta_str} "
                f"| {s.phase} "
                f"| {s.flow_5d_100m:+.1f} "
                f"| {s.pop_concentration:.0%} "
                f"| {s.nextday_risk_penalty:.0f} |"
            )

    # ---- 退潮预警（avoid）----
    if avoid_sectors:
        lines += [
            "",
            "### 🔴 退潮预警（建议回避）",
            "",
            "| 板块 | 决策分 | 5日资金(亿) | MA20广度 | 人气集中 | 次日风险 |",
            "|---|---|---|---|---|---|",
        ]
        for s in avoid_sectors:
            lines.append(
                f"| {s.industry} | {s.decision_score} "
                f"| {s.flow_5d_100m:+.1f} "
                f"| {s.pct_above_ma20:.0%} "
                f"| {s.pop_concentration:.0%} "
                f"| {s.nextday_risk_penalty:.0f} |"
            )

    # ---- LLM新闻主题（Phase 2 已接入）----
    if state.themes:
        lines += ["", "### 📰 LLM新闻主题催化"]
        lines += [
            "",
            "| 主题 | 热度 | 阶段 | 关联行业 | 证据 |",
            "|---|---|---|---|---|",
        ]
        for t in sorted(state.themes, key=lambda x: x.heat, reverse=True)[:8]:
            industries = "、".join(t.concept_codes[:2]) or "—"
            evidence = t.evidence[0] if t.evidence else "—"
            lines.append(
                f"| **{t.name}** | {t.heat:.1f} | {t.phase} "
                f"| {industries} | {evidence} |"
            )
    else:
        lines.append("")
        lines.append("_（今日无财联社新闻或LLM主题分析不可用）_")


def _append_candidates_table(lines: list, candidates: list[Candidate]) -> None:
    """写入候选股总览表（含止损止盈）。"""
    lines.append("")
    lines.append(
        "| # | 代码 | 名称 | 行业 | 市值(亿) | 振幅 | 涨跌幅 | "
        "主力净流(万) | RPS50 | 保守买入 | 激进买入 | 止损 | 止盈1 | 止盈2 | 仓位 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(candidates, 1):
        f = c.factors
        p = c.trade_plan
        lines.append(
            f"| {i} | {c.code} | {c.name} | {c.theme} "
            f"| {f.market_cap:.0f} | {f.avg_amplitude_5d:.1f}% "
            f"| {f.pct_change:+.2f}% | {f.fund_flow_3d:+.0f} "
            f"| {f.rps50:.0f} "
            f"| **{p.buy_conservative:.2f}** | {p.buy_aggressive:.2f} "
            f"| 🛑{p.stop_loss:.2f} | 🎯{p.take_profit_1:.2f} | 🎯{p.take_profit_2:.2f} "
            f"| {p.position_pct:.0%} |"
        )


def _append_stock_detail(lines: list, idx: int, c: Candidate, market_label: str) -> None:
    """写入单只股票的详情区块。"""
    p = c.trade_plan
    f = c.factors

    lines += [
        "",
        f"### {idx}. {c.name}（{c.code}）｜{c.theme}",
        "",
        "**因子通过：** " + "  |  ".join(c.filters_passed),
    ]

    if c.filters_failed:
        lines.append("**因子提示：** " + "  |  ".join(c.filters_failed[:3]))

    # 走势摘要
    if c.trend_summary:
        lines.append("")
        lines.append(c.trend_summary)

    # 交易计划
    lines += [
        "",
        "**📋 交易执行计划**",
        "",
        "| 操作 | 价格 | 说明 |",
        "|---|---|---|",
        f"| 保守买入 | **{p.buy_conservative:.2f}** | 20日VWAP主力成本区，回踩首选 |",
        f"| 激进买入 | {p.buy_aggressive:.2f} | 当日收盘价，趋势追随 |",
        f"| 🛑 止损 | {p.stop_loss:.2f} | 跌破MA5无条件止损 |",
        f"| 🎯 止盈1 | {p.take_profit_1:.2f} | +5% 减仓一半 |",
        f"| 🎯 止盈2 | {p.take_profit_2:.2f} | +8% 继续减仓 |",
        f"| 建议仓位 | {p.position_pct:.0%} | 基于{market_label}市场状态 |",
    ]

    # 次日观察清单
    if p.execution_checklist:
        lines.append("")
        lines.append(p.execution_checklist)

    lines.append("")
    lines.append("---")


def _save_report(trade_date: str, content: str) -> None:
    """将报告保存到 reports/<trade_date>.md。"""
    settings = get_settings()
    path: Path = settings.report_dir / f"{trade_date}.md"
    path.write_text(content, encoding="utf-8")
    logger.info("报告已保存: %s", path)
