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
    """生成 Markdown 报告并写入文件，并触发历史胜率追踪（O13）。"""
    logger.info("[节点E] 报告生成")

    # 数据缺失时跳过报告写入，避免生成空报告污染历史列表
    if state.market_regime and state.market_regime.label == "数据缺失":
        logger.warning("[节点E] 大盘数据缺失（非交易日或数据未就绪），跳过报告生成")
        return state

    report = _build_report(state)
    state.report_md = report
    _save_report(state.trade_date, report)
    _track_candidates(state)
    return state


def _track_candidates(state: PipelineState) -> None:
    """前向追踪：将今日候选股快照写入 strategy.db（entry_price 次日回填）。"""
    if not state.candidates:
        return
    try:
        from app.strategy.forward_tracker import save_today_candidates
        save_today_candidates(
            trade_date=state.trade_date,
            candidates=state.candidates,
            market_label=state.market_regime.label,
        )
    except Exception as e:
        logger.warning("[前向追踪] 快照写入失败（不影响报告）: %s", e)


def _append_tracking_section(lines: list, state: PipelineState) -> None:
    """在报告末尾附加前向追踪实际表现区块。"""
    try:
        from app.strategy.forward_tracker import get_tracking_report_section
        section = get_tracking_report_section(state.trade_date)
        if section:
            lines.append(section)
    except Exception as e:
        logger.debug("[前向追踪] 报告区块生成失败（非关键）: %s", e)


def _build_report(state: PipelineState) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    regime = state.market_regime
    meta = state.meta

    # 将 YYYYMMDD 格式化为 YYYY-MM-DD 便于阅读
    td = state.trade_date
    td_display = f"{td[:4]}-{td[4:6]}-{td[6:]}"

    lines = [
        f"# A股每日选股简报",
        f"> 📅 **数据交易日：{td_display}**　|　报告生成：{now}",
        f"> ⚠️ 定位：信息聚合+量化初筛，不构成投资建议",
        "",
    ]

    # ---- 第一部分：大盘择时 ----
    _append_market_section(lines, regime)

    # ---- 第二部分：板块热度（Phase 2 量化）----
    _append_sector_section(lines, state)

    # ---- 行情联动分析（O7）----
    _append_market_linkage(lines, state)

    # ---- 第三部分：候选股总览表 ----
    can_open = regime.can_open
    if not can_open:
        lines += [
            "",
            "## 三、候选股票池（观察模式）",
            f"> 🔴 当前市场状态【{regime.label}】，仓位建议 **0%，不实际买入**。",
            "> 以下为量化筛选的潜力股观察池，供市场好转后参考。",
        ]
    else:
        lines += ["", "## 三、候选股票池"]

    if state.candidates:
        _append_candidates_table(lines, state.candidates)
        lines.append("")
        lines.append(
            "> ⚠️ **免责**：量化初筛结果，需自行核查基本面"
            "（扣非净利润连续2季度为正、商誉<净资产30%、无大股东减持公告）。"
            "止损止盈为系统建议，不构成买入建议。"
        )

        # ---- 网络舆情避雷（博查实时检索，真实原文核对）----
        _append_news_guard_section(lines, state.candidates)

        # ---- 第四部分：逐股详情（走势+交易计划+次日清单）----
        lines += ["", "## 四、个股详情与执行计划"]
        for i, c in enumerate(state.candidates, 1):
            _append_stock_detail(lines, i, c, regime.label)
    else:
        lines.append("_（今日无股票通过量化筛选）_")

    # ---- AI 深度复盘（v4-pro 接地：大盘→主线→为何选→风险）----
    _append_llm_review(lines, state)

    # ---- O15: 持仓追踪区块 ----
    _append_tracking_section(lines, state)

    # ---- 盘后复盘区块（板块涨跌榜 + 观察池表现）----
    _append_post_market_section(lines, state)

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
    """写入大盘择时区块（含情绪分和置信度）。"""
    state_emoji = {
        "主升": "🚀", "升温": "📈", "震荡": "➡️",
        "退潮反抽": "⚠️", "弱势": "📉", "衰退": "🔴",
    }.get(regime.label, "❓")

    # 情绪分颜色
    emo = regime.emotion_score
    emo_label = "极度乐观" if emo >= 80 else ("乐观" if emo >= 60 else ("中性" if emo >= 40 else ("悲观" if emo >= 20 else "极度恐慌")))
    emo_emoji = "🟢" if emo >= 60 else ("🟡" if emo >= 40 else "🔴")

    lines += [
        "## 一、大盘择时",
        f"### {state_emoji} 市场状态：{regime.label}　|　"
        f"{'✅ 可开仓' if regime.can_open else '❌ 暂停开仓'}　|　"
        f"仓位上限 {regime.position_limit:.0%}　|　"
        f"置信度 {regime.confidence:.0%}",
        "",
        f"> {emo_emoji} **市场情绪综合分：{emo:.0f}/100**（{emo_label}）",
        "",
        "| 指标 | 数值 | 信号 |",
        "|---|---|---|",
        f"| 涨停家数 | {regime.limit_up_count} 家 | "
        f"{'✅ 强' if regime.limit_up_count >= 80 else '⚠️ 弱'} |",
        f"| 跌停家数 | {regime.limit_down_count} 家 | "
        f"{'✅ 安全' if regime.limit_down_count < 15 else '❌ 危险'} |",
        f"| 全市场下跌家数 | {regime.down_count} 家 | "
        f"{'✅ 可控' if regime.down_count < 2000 else '⚠️ 偏多'} |",
        f"| 连板最高板数 | {regime.consecutive_limit_high} 板 | "
        f"{'✅ 强赚钱效应' if regime.consecutive_limit_high >= 5 else ('🟡 一般' if regime.consecutive_limit_high >= 3 else '⚠️ 弱')} |",
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
        f"✅ 可关注 **{len(buy_sectors)}** 个 | "
        f"👀 观望 **{len(watch_sectors)}** 个 | "
        f"⛔ 退潮预警 **{len(avoid_sectors)}** 个",
    ]

    # ---- 全市场板块衰减占比（对标吴川 decay_ratio，市场级风险闸门）----
    from app.sector_analyzer import calc_decay_ratio
    dr = calc_decay_ratio(sector_stats)
    if dr["decay_ratio"] is not None:
        emoji = {"defensive": "🔴", "cautious": "🟠", "normal": "🟢"}.get(dr["level"], "⚪")
        lines += [
            "",
            f"> {emoji} **板块衰减占比 {dr['decay_ratio']:.0%}**"
            f"（退潮 {dr['n_decay']}/{dr['n_total']} 个）→ {dr['advice']}",
        ]

    # ---- 可关注板块（buy）----
    if buy_sectors:
        lines += [
            "",
            f"### ✅ 可关注板块（决策评分 ≥ 60，共 {len(buy_sectors)} 个）",
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
            "### 👀 观望板块（决策评分 40-59）",
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
            "### ⛔ 退潮预警（建议回避）",
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

    # ---- O5: 隔夜风险总览表 ----
    _append_overnight_risk_table(lines, sector_stats)

    # ---- LLM新闻主题（Phase 2 已接入，含龙头股）----
    if state.themes:
        lines += ["", "### 📰 LLM新闻主题催化"]
        # O13: 加载历史胜率（有数据则显示，首次运行为空）
        win_rates: dict = {}
        try:
            from app.history_tracker import get_theme_win_rates
            win_rates = get_theme_win_rates(min_samples=3)
        except Exception:
            pass
        for t in sorted(state.themes, key=lambda x: x.heat, reverse=True)[:8]:
            phase_emoji = {"事件驱动↑": "⚡", "升温": "🔥", "趋势": "↗", "退潮": "📉"}.get(t.phase, "—")
            industries = "、".join(t.concept_codes[:3]) or "—"
            evidence = " / ".join(t.evidence[:2]) if t.evidence else "—"
            # O13: 历史胜率标签（有历史数据时显示）
            wr_info = win_rates.get(t.name)
            wr_str = (
                f"　历史T+1胜率 {wr_info['win_rate']*100:.0f}%({wr_info['samples']}次)"
                if wr_info else ""
            )
            lines += [
                "",
                f"**{phase_emoji} {t.name}**　热度 {t.heat:.1f}/10　{t.phase}　关联行业：{industries}{wr_str}",
                f"> {evidence}",
            ]
            # 龙头股
            if t.leaders:
                leader_cells = []
                for l in t.leaders:
                    flag = "🚀" if l.is_limit_up else ("📈" if l.pct_change > 0 else "📉")
                    flow_str = f"主力{l.fund_flow/10000:+.1f}亿" if abs(l.fund_flow) > 1000 else ""
                    leader_cells.append(f"{flag} **{l.name}**({l.code[:6]}) {l.pct_change:+.1f}% RPS{l.rps50:.0f} {flow_str}")
                lines.append("　".join(leader_cells))
    else:
        lines.append("")
        lines.append("_（今日无新闻数据或LLM主题分析不可用）_")


def _append_market_linkage(lines: list, state: "PipelineState") -> None:
    """
    O7: 行情联动分析区块。
    分三类：潜伏改善（量化+热度上升） / 风险主题（退潮+加速下行） / 领涨龙头（主题+量化双验证）
    数据来源：sector_stats（量化）+ themes（LLM主题龙头）
    """
    sector_stats = state.sector_stats
    themes = state.themes

    # ---- 潜伏改善：阶段升温/趋势 且 3日热度上升 ----
    improving = [
        s for s in sector_stats
        if s.phase in ("升温", "趋势") and s.heat_score_delta_3d > 5 and s.decision in ("buy", "watch")
    ]
    improving.sort(key=lambda x: x.heat_score_delta_3d, reverse=True)

    # ---- 风险主题：退潮 且 3日加速下行 ----
    risky = [
        s for s in sector_stats
        if s.phase == "退潮" and s.heat_score_delta_3d < -8
    ]
    risky.sort(key=lambda x: x.heat_score_delta_3d)

    # ---- 领涨龙头：来自热门主题 + 当日强势 ----
    leaders = []
    for t in sorted(themes, key=lambda x: x.heat, reverse=True)[:5]:
        if t.heat >= 5.0 and t.leaders:
            top = t.leaders[0]
            if top.pct_change > 5 or top.is_limit_up:
                leaders.append((t.name, t.phase, top))

    if not (improving or risky or leaders):
        return

    lines += ["", "## 二点五、行情联动分析"]

    # 领涨龙头
    if leaders:
        lines += ["", "#### 🔥 领涨龙头（新闻+量化双验证）"]
        for theme_name, phase, l in leaders[:4]:
            flag = "🚀" if l.is_limit_up else "📈"
            flow_str = f" 主力{l.fund_flow/10000:+.1f}亿" if abs(l.fund_flow) > 500 else ""
            lines.append(
                f"- **{l.name}**({l.code[:6]}) {flag}{l.pct_change:+.1f}%　"
                f"主题[{theme_name}/{phase}]{flow_str}"
            )

    # 潜伏改善
    if improving:
        lines += ["", "#### 📈 潜伏改善（资金回流/量化升温）"]
        for s in improving[:5]:
            delta = f"+{s.heat_score_delta_3d:.0f}" if s.heat_score_delta_3d > 0 else f"{s.heat_score_delta_3d:.0f}"
            lines.append(
                f"- **{s.industry}**：热度{s.heat_score:.0f}，3日变化{delta}，"
                f"5日资金{s.flow_5d_100m:+.1f}亿，{s.phase}。"
                f"（{'等待回调确认，不追高' if s.pop_concentration > 0.3 else '可关注低吸机会'}）"
            )

    # 风险主题
    if risky:
        lines += ["", "#### ⚠️ 风险主题（涨跌+拥挤度）"]
        for s in risky[:5]:
            lines.append(
                f"- **{s.industry}**：热度{s.heat_score:.0f}，3日变化{s.heat_score_delta_3d:.0f}，"
                f"5日资金{s.flow_5d_100m:+.1f}亿，人气集中{s.pop_concentration:.0%}。"
                f"退潮加速，建议回避。"
            )


def _append_overnight_risk_table(lines: list, sector_stats: list) -> None:
    """
    O5: 隔夜风险总览表（对标吴川报告格式）。
    汇总所有热门板块的次日关键风险指标，供睡前一眼扫描。
    """
    if not sector_stats:
        return

    # 只显示热度>50 或 有风险的板块
    candidates = [s for s in sector_stats if s.heat_score > 50 or s.nextday_risk_penalty >= 60]
    if not candidates:
        return

    # 按次日风险从高到低排序（风险高的优先提示）
    candidates = sorted(candidates, key=lambda x: x.nextday_risk_penalty, reverse=True)[:12]

    lines += [
        "",
        "### 🌙 隔夜风险总览",
        "",
        "| 板块 | 阶段 | 热度 | 趋势分 | 3日Δ | 5日资金(亿) | 人气集中 | 次日风险 | 风控提示 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for s in candidates:
        delta_str = f"{s.heat_score_delta_3d:+.0f}" if s.heat_score_delta_3d != 0 else "—"
        risk = s.nextday_risk_penalty

        if risk >= 80:
            tip = "⛔ 高风险，严格回避"
        elif risk >= 60:
            tip = "⚠️ 次日需条件确认"
        elif risk >= 30:
            tip = "🟡 相对可控，仍需观察"
        else:
            tip = "✅ 风险较低"

        # 人气集中度过高额外标注
        if s.pop_concentration > 0.5:
            tip += "，⚠️极度拥挤"
        elif s.pop_concentration > 0.3:
            tip += "，注意拥挤"

        lines.append(
            f"| {s.industry} | {s.phase} | {s.heat_score:.0f} | {s.trend_score:.0f} | {delta_str} "
            f"| {s.flow_5d_100m:+.1f} | {s.pop_concentration:.0%} "
            f"| **{risk:.0f}** | {tip} |"
        )


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
    # 新因子信号标注
    rsi_note = f"🔴过热" if f.rsi_14 > 70 else ("🟢超卖" if f.rsi_14 < 30 else "")
    vwap_note = f"⚠️偏离+{f.vwap_deviation:.0f}%（追高风险）" if f.vwap_deviation > 15 else (
        f"✅低于主力成本{f.vwap_deviation:.0f}%" if f.vwap_deviation < -5 else ""
    )
    pct7_note = f"⚠️7日+{f.change_pct_7d:.1f}%（短期过热）" if f.change_pct_7d > 15 else ""

    lines += [
        "",
        f"### {idx}. {c.name}（{c.code}）｜{c.theme}",
        "",
        "**因子通过：** " + "  |  ".join(c.filters_passed),
    ]

    # 新因子快览行
    new_factor_parts = []
    if f.rsi_14 > 0:
        new_factor_parts.append(f"RSI_14={f.rsi_14:.0f}{' '+rsi_note if rsi_note else ''}")
    if f.vwap_deviation != 0:
        new_factor_parts.append(f"VWAP偏离={f.vwap_deviation:+.1f}%{' '+vwap_note if vwap_note else ''}")
    if f.change_pct_7d != 0:
        new_factor_parts.append(f"7日涨幅={f.change_pct_7d:+.1f}%{' '+pct7_note if pct7_note else ''}")
    if f.popularity_rank > 0:
        new_factor_parts.append(f"人气排名={f.popularity_rank}名")
    if new_factor_parts:
        lines.append("**新增指标：** " + "  |  ".join(new_factor_parts))

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
        f"| 🛑 止损位 | {p.stop_loss:.2f} | 跌破即离场（多条件见下） |",
        f"| 🎯 止盈1 | {p.take_profit_1:.2f} | +5% 减仓一半 |",
        f"| 🎯 止盈2 | {p.take_profit_2:.2f} | +8% 继续减仓 |",
        f"| 建议仓位 | {p.position_pct:.0%} | 基于{market_label}市场状态 |",
    ]

    # 多条件止损 + 次日验证清单（对标吴川，量化可勾选）
    from app.factors.trade_rules import build_stop_rule, build_nextday_checklist
    lines += [
        "",
        f"**🛑 多条件止损：** {build_stop_rule(p.stop_loss, c.theme)}",
        "",
        "**🌅 次日 09:30-09:40 验证清单（满足才介入）：**",
    ]
    for i, rule in enumerate(build_nextday_checklist(f.close), 1):
        lines.append(f"{i}. {rule}")

    # 多空辩论结果
    if c.debate:
        lines.append("")
        verdict_emoji = "✅" if c.debate.verdict == "通过" else "❌"
        lines.append(f"**{verdict_emoji} 风控裁决：{c.debate.verdict}** — {c.debate.verdict_reason}")
        if c.debate.bull_points:
            lines.append("")
            lines.append("**📈 多头论点：**")
            for pt in c.debate.bull_points:
                lines.append(f"- {pt}")
        if c.debate.bear_points:
            lines.append("")
            lines.append("**📉 空头风险：**")
            for pt in c.debate.bear_points:
                lines.append(f"- {pt}")

    # 次日观察清单
    if p.execution_checklist:
        lines.append("")
        lines.append(p.execution_checklist)

    lines.append("")
    lines.append("---")


def _append_llm_review(lines: list, state: PipelineState) -> None:
    """
    AI 深度复盘（v4-pro 接地）：大盘环境 → 主线板块 → 为何选这些候选(横向) → 风险。
    只解释已算因子+真实数据，不编造、不预测涨跌、不输出胜率。盘后批量，无人等待用 pro。
    """
    if not state.candidates:
        return
    try:
        from app.llm.client import LLMClient
        regime = state.market_regime
        buy_sectors = [s for s in state.sector_stats if s.decision == "buy"][:6]
        sec_txt = "、".join(
            f"{s.industry}(热度{s.heat_score:.0f}/{s.phase}/5日资金{s.flow_5d_100m:+.1f}亿)"
            for s in buy_sectors
        ) or "无明确主线（震荡轮动）"

        cand_lines = []
        for c in state.candidates[:10]:
            f = c.factors
            strat = c.filters_passed[-1] if c.filters_passed else ""
            risk = "｜⚠️" + "；".join(c.risk_flags) if c.risk_flags else ""
            cand_lines.append(
                f"- {c.name}({c.code[:6]}) {c.theme} [{strat}] "
                f"RPS{f.rps50:.0f} 3日主力{f.fund_flow_3d/1e4:+.1f}亿 7日{f.change_pct_7d:+.1f}% "
                f"仓位{c.trade_plan.position_pct:.0%}{risk}"
            )
        cand_txt = "\n".join(cand_lines)

        td = state.trade_date
        prompt = (
            f"你是A股策略总监。基于 {td[:4]}-{td[4:6]}-{td[6:]} 的真实盘后数据，写一段 150-280 字的深度复盘。"
            f"**只依据下方数据，严禁编造未出现的公司/数字/事件，不预测涨跌，不输出胜率/成功率，不构成投资建议。**\n"
            f"结构（连贯成段，不分点标题）：①大盘环境与赚钱效应 ②今日主线板块 "
            f"③为何选中这些候选（结合板块热度/资金/多路验证，可横向点评同主题强弱）④风险提示（含避雷）。\n\n"
            f"【大盘】状态{regime.label}｜情绪{regime.emotion_score:.0f}/100｜"
            f"涨停{regime.limit_up_count}/跌停{regime.limit_down_count}｜连板最高{regime.consecutive_limit_high}板｜{regime.reason}\n"
            f"【主线板块(可关注)】{sec_txt}\n"
            f"【今日候选】\n{cand_txt}\n"
        )
        review = LLMClient().chat(
            [{"role": "user", "content": prompt}], task_type="pro", max_tokens=1500,
        ).strip()
        if review:
            lines += ["", "## 🧠 AI 深度复盘（v4-pro·接地）", "", review,
                      "", "> ⚠️ AI 基于客观因子的解读，不构成投资建议。"]
    except Exception as e:
        logger.debug("[节点E] AI深度复盘生成失败（不影响报告）: %s", e)


def _append_news_guard_section(lines: list, candidates: list[Candidate]) -> None:
    """
    候选股网络舆情避雷区块（博查实时检索）。

    准确性优先：直接呈现真实新闻原文（标题+来源+日期+可点击链接），
    不经 LLM 复述；命中风险关键词的股票逐条列出，未命中的集中标注「未检索到明显负面」。
    未配置博查 key 时整段跳过（不显示）。
    """
    try:
        from app.strategy.news_guard import scan_candidates
        hits_map = scan_candidates(candidates)
    except Exception as e:
        logger.debug("[避雷] 网络舆情检索失败（不影响报告）: %s", e)
        return

    # scan_candidates 在未启用博查时返回 {}；此时不显示该区块
    from app.data.web_search import BochaSearchClient
    if not BochaSearchClient().enabled:
        return

    lines += [
        "",
        "## 🛡️ 候选股网络舆情避雷（博查实时检索）",
        "> ⚠️ 自动检索近一月全网新闻，命中风险关键词即列出**原文供人工核实**，"
        "与系统结构化避雷互补；非确定性结论，请点击来源核对。",
        "",
    ]

    flagged = [c for c in candidates if str(c.code)[:6] in hits_map]
    clean = [c for c in candidates if str(c.code)[:6] not in hits_map]

    if flagged:
        for c in flagged:
            code6 = str(c.code)[:6]
            lines.append(f"**⚠️ {c.name}（{code6}）** — 检索到潜在负面：")
            for h in hits_map[code6]:
                meta = " · ".join(x for x in [h["date"], h["site"]] if x)
                link = f"[{h['title']}]({h['url']})" if h["url"] else h["title"]
                lines.append(f"- 〔{h['keyword']}〕{link}" + (f"（{meta}）" if meta else ""))
            lines.append("")
    else:
        lines.append("✅ 候选股均未检索到明显负面舆情。")
        lines.append("")

    if clean:
        names = "、".join(f"{c.name}({str(c.code)[:6]})" for c in clean)
        lines.append(f"> ✅ 未检索到明显负面：{names}")
        lines.append("")


def _append_post_market_section(lines: list, state: "PipelineState") -> None:
    """
    盘后复盘区块：板块资金动向 + 观察池近期表现。
    数据来自已有 sector_stats 和 strategy.db，不额外调用 API。
    """
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 📊 盘后复盘")
    lines.append("")

    # ---- 板块资金动向（热度变化量排序）----
    stats = state.sector_stats
    if stats:
        sorted_up = sorted(stats, key=lambda s: s.heat_score_delta_3d, reverse=True)[:5]
        sorted_dn = sorted(stats, key=lambda s: s.heat_score_delta_3d)[:5]

        lines.append("### 🏆 今日板块资金动向")
        lines.append("")
        lines.append("**🔴 资金流入 Top5**")
        lines.append("")
        lines.append("| 板块 | 热度 | 3日Δ | 阶段 | 5日净流入(亿) |")
        lines.append("|---|---|---|---|---|")
        for s in sorted_up:
            flow = f"+{s.flow_5d_100m:.1f}" if s.flow_5d_100m >= 0 else f"{s.flow_5d_100m:.1f}"
            delta = f"+{s.heat_score_delta_3d:.1f}" if s.heat_score_delta_3d >= 0 else f"{s.heat_score_delta_3d:.1f}"
            lines.append(
                f"| {s.industry} | {s.heat_score:.0f} | {delta} "
                f"| {s.phase} | {flow} |"
            )
        lines.append("")
        lines.append("**🟢 资金流出 Top5**")
        lines.append("")
        lines.append("| 板块 | 热度 | 3日Δ | 阶段 | 5日净流入(亿) |")
        lines.append("|---|---|---|---|---|")
        for s in sorted_dn:
            flow = f"+{s.flow_5d_100m:.1f}" if s.flow_5d_100m >= 0 else f"{s.flow_5d_100m:.1f}"
            delta = f"+{s.heat_score_delta_3d:.1f}" if s.heat_score_delta_3d >= 0 else f"{s.heat_score_delta_3d:.1f}"
            lines.append(
                f"| {s.industry} | {s.heat_score:.0f} | {delta} "
                f"| {s.phase} | {flow} |"
            )
        lines.append("")

    # ---- 观察池近期表现 ----
    lines.append("### 👁️ 观察池近期表现（近5日）")
    lines.append("")
    try:
        from app.strategy.forward_tracker import get_recent_watchlist_perf
        perfs = get_recent_watchlist_perf(state.trade_date, days=7)
        if perfs:
            lines.append("| 名称 | 代码 | 选股日 | 市场 | 止损价 | T+1收益 | 止损触发 |")
            lines.append("|---|---|---|---|---|---|---|")
            for p in perfs:
                pct = f"{p['pct_return']:+.2f}%" if p["pct_return"] is not None else "待回填"
                stop_flag = "🛑是" if p["hit_stop_loss"] else "否"
                lines.append(
                    f"| {p['name']} | {p['code']} | {p['run_date']} "
                    f"| {p['market_label']} | {p['stop_loss']:.2f} "
                    f"| {pct} | {stop_flag} |"
                )
        else:
            lines.append("_（近期暂无实盘追踪记录）_")
    except Exception as e:
        logger.warning("盘后复盘 - 观察池数据读取失败: %s", e)
        lines.append("_（数据读取失败）_")
    lines.append("")


def _save_report(trade_date: str, content: str) -> None:
    """将报告保存到 reports/<trade_date>.md。"""
    settings = get_settings()
    path: Path = settings.report_dir / f"{trade_date}.md"
    path.write_text(content, encoding="utf-8")
    logger.info("报告已保存: %s", path)
