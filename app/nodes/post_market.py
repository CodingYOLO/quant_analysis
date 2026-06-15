"""
盘后复盘模块（Post-Market Review）。

职责：
  - 今日全市场板块涨跌榜（Top5涨 / Top5跌）
  - 今日北向资金净流入
  - 今日主力资金净流向（全市场汇总）
  - 观察池个股今日实际表现（T+1回填）
  - 输出 Markdown 字符串，追加到主报告末尾

调用时机：盘后 16:05 流水线中，在主报告生成后调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.state import PipelineState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class SectorPerf:
    """单个板块今日表现。"""
    name: str
    pct_chg: float          # 板块平均涨跌幅（%）
    net_flow_bn: float      # 主力净流入（亿元），无数据时为 None
    up_count: int           # 上涨家数
    total_count: int        # 板块总家数


@dataclass
class WatchlistPerf:
    """观察池个股今日实际表现。"""
    code: str
    name: str
    open_pct: float         # 开盘涨跌幅（相对昨收）
    close_pct: float        # 收盘涨跌幅
    hit_stop_loss: bool     # 是否触及止损价
    stop_loss_price: float  # 止损价


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def build_post_market_section(state: PipelineState, provider: CompositeProvider) -> str:
    """
    生成盘后复盘 Markdown 区块。

    Args:
        state: 当日流水线状态（含 sector_stats、candidates）
        provider: 数据提供者

    Returns:
        Markdown 字符串，可直接追加到主报告
    """
    lines: list[str] = []
    lines.append("\n---\n")
    lines.append("## 📊 盘后复盘\n")

    _append_market_summary(lines, state, provider)
    _append_sector_rank(lines, state, provider)
    _append_watchlist_perf(lines, state, provider)

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 内部子模块
# --------------------------------------------------------------------------- #

def _append_market_summary(
    lines: list[str],
    state: PipelineState,
    provider: CompositeProvider,
) -> None:
    """今日全市场概况：涨跌家数、北向、主力资金。"""
    regime = state.market_regime
    trade_date = state.trade_date

    # 涨跌家数来自 market_regime（已由节点A计算）
    up_count = regime.limit_up_count or 0
    down_count_total = regime.down_count or 0
    limit_down = regime.limit_down_count or 0

    # 北向资金
    north_text = "数据缺失"
    try:
        df_north = provider.get_north_flow(trade_date)
        if df_north is not None and not df_north.empty:
            north_val = float(df_north["north_money"].iloc[0])
            sign = "+" if north_val >= 0 else ""
            north_text = f"{sign}{north_val / 10000:.1f}亿元 {'🟢净流入' if north_val >= 0 else '🔴净流出'}"
    except Exception as e:
        logger.debug("获取北向资金失败: %s", e)

    # 主力资金净流向（汇总 moneyflow 的 net_mf_amount）
    mf_text = "数据缺失"
    try:
        df_mf = provider.get_money_flow(trade_date)
        if df_mf is not None and not df_mf.empty and "net_mf_amount" in df_mf.columns:
            total_net = df_mf["net_mf_amount"].sum() / 10000  # 转亿元
            sign = "+" if total_net >= 0 else ""
            mf_text = f"{sign}{total_net:.0f}亿元 {'🟢流入' if total_net >= 0 else '🔴流出'}"
    except Exception as e:
        logger.debug("获取主力资金失败: %s", e)

    lines.append("### 📈 今日市场概况\n")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 涨停家数 | {up_count} 家 |")
    lines.append(f"| 跌停家数 | {limit_down} 家 |")
    lines.append(f"| 全市场下跌家数 | {down_count_total} 家 |")
    lines.append(f"| 北向资金 | {north_text} |")
    lines.append(f"| 全市场主力资金 | {mf_text} |")
    lines.append("")


def _append_sector_rank(
    lines: list[str],
    state: PipelineState,
    provider: CompositeProvider,
) -> None:
    """
    今日板块涨跌榜：Top5涨 / Top5跌。
    数据来源：sector_stats（已由节点B计算，含热度/资金）。
    涨跌幅用各板块成员股平均 pct_chg 估算。
    """
    if not state.sector_stats:
        lines.append("_（板块数据缺失）_\n")
        return

    # 用 sector_stats 里的 heat_score 变化量近似代表今日强弱
    # 更准确的做法是用 pct_chg 字段，检查是否存在
    stats = state.sector_stats

    # 尝试从 SectorStat 拿 avg_pct_chg（如果有）
    has_pct = hasattr(stats[0], "avg_pct_chg") and stats[0].avg_pct_chg is not None

    if has_pct:
        sorted_stats = sorted(stats, key=lambda s: s.avg_pct_chg, reverse=True)
        top5_up = sorted_stats[:5]
        top5_down = sorted_stats[-5:][::-1]

        lines.append("### 🏆 今日板块涨跌榜\n")
        lines.append("**🟢 涨幅榜 Top5**\n")
        lines.append("| 板块 | 涨跌幅 | 阶段 | 5日净流入(亿) |")
        lines.append("|---|---|---|---|")
        for s in top5_up:
            flow = f"+{s.net_flow_5d_bn:.1f}" if s.net_flow_5d_bn >= 0 else f"{s.net_flow_5d_bn:.1f}"
            lines.append(f"| {s.name} | {s.avg_pct_chg:+.2f}% | {s.stage} | {flow} |")

        lines.append("")
        lines.append("**🔴 跌幅榜 Top5**\n")
        lines.append("| 板块 | 涨跌幅 | 阶段 | 5日净流入(亿) |")
        lines.append("|---|---|---|---|")
        for s in top5_down:
            flow = f"+{s.net_flow_5d_bn:.1f}" if s.net_flow_5d_bn >= 0 else f"{s.net_flow_5d_bn:.1f}"
            lines.append(f"| {s.name} | {s.avg_pct_chg:+.2f}% | {s.stage} | {flow} |")
        lines.append("")
    else:
        # 退化方案：用热度变化量（3日Δ）排序，反映今日资金方向
        sorted_stats = sorted(stats, key=lambda s: s.heat_delta_3d, reverse=True)
        top5_up = [s for s in sorted_stats if s.heat_delta_3d > 0][:5]
        top5_down = [s for s in sorted_sorted if s.heat_delta_3d < 0][-5:][::-1] if False else \
                    sorted(stats, key=lambda s: s.heat_delta_3d)[:5]

        lines.append("### 🏆 今日板块资金动向（热度变化）\n")
        lines.append("**🟢 资金流入 Top5**\n")
        lines.append("| 板块 | 热度3日Δ | 阶段 | 5日净流入(亿) |")
        lines.append("|---|---|---|---|")
        for s in top5_up:
            flow = f"+{s.net_flow_5d_bn:.1f}" if s.net_flow_5d_bn >= 0 else f"{s.net_flow_5d_bn:.1f}"
            lines.append(f"| {s.name} | +{s.heat_delta_3d:.1f} | {s.stage} | {flow} |")

        lines.append("")
        lines.append("**🔴 资金流出 Top5**\n")
        lines.append("| 板块 | 热度3日Δ | 阶段 | 5日净流入(亿) |")
        lines.append("|---|---|---|---|")
        for s in top5_down:
            flow = f"+{s.net_flow_5d_bn:.1f}" if s.net_flow_5d_bn >= 0 else f"{s.net_flow_5d_bn:.1f}"
            lines.append(f"| {s.name} | {s.heat_delta_3d:.1f} | {s.stage} | {flow} |")
        lines.append("")


def _append_watchlist_perf(
    lines: list[str],
    state: PipelineState,
    provider: CompositeProvider,
) -> None:
    """
    观察池个股今日实际表现（从 strategy.db 前向追踪记录读取）。
    显示：开盘价、收盘价、涨跌幅、是否触及止损。
    """
    lines.append("### 👁️ 观察池今日表现\n")
    try:
        from app.strategy.forward_tracker import get_recent_watchlist_perf
        rows = get_recent_watchlist_perf(state.trade_date, days=5)
        if not rows:
            lines.append("_（近5日无观察池记录）_\n")
            return

        lines.append("| 名称 | 代码 | 选股日 | 市场 | 止损价 | 今日收益 | 止损触发 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in rows:
            pct = f"{r['pct_return']:+.2f}%" if r["pct_return"] is not None else "待回填"
            stop = "🛑是" if r["hit_stop_loss"] else "否"
            lines.append(
                f"| {r['name']} | {r['code']} | {r['run_date']} "
                f"| {r['market_label']} | {r['stop_loss']:.2f} | {pct} | {stop} |"
            )
        lines.append("")
    except Exception as e:
        logger.warning("观察池表现读取失败: %s", e)
        lines.append("_（数据读取失败）_\n")
