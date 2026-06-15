"""
Server酱手机简报生成器。

设计原则：手机屏幕空间有限，只推"必须知道"的信息。
  - 标题：当日市场状态 + 一句话判断
  - 正文：≤ 10 行，用 emoji 快速扫读
  - 完整报告在 Web 查看，不在手机展开

适用场景：盘前提示 + 盘后复盘 均调用此模块生成摘要。
"""

from __future__ import annotations

from app.state import PipelineState


def build_pre_market_summary(state: PipelineState, web_url: str = "") -> tuple[str, str]:
    """
    盘前简报（手机推送版）。

    Returns:
        (title, content) — title ≤ 32字，content 为 Markdown 正文
    """
    regime = state.market_regime
    trade_date = _fmt_date(state.trade_date)

    title = f"【盘前】{trade_date} {regime.label} | {_position_hint(regime)}"

    lines: list[str] = []

    # 市场状态一行
    lines.append(f"**市场：{regime.label}** 　仓位上限 {int(regime.position_limit * 100)}%")
    lines.append(f"> {regime.reason}")
    lines.append("")

    # 关键指标
    lines.append(
        f"📊 涨停{regime.limit_up_count}家 | 跌停{regime.limit_down_count}家 | "
        f"MA5占比{regime.pct_above_ma5:.0%}"
    )
    north = regime.north_net_million
    if north is not None:
        sign = "+" if north >= 0 else ""
        lines.append(f"🌊 北向资金 {sign}{north/10000:.1f}亿 {'净流入' if north >= 0 else '净流出'}")
    lines.append("")

    # 候选股执行清单（最多3只）
    candidates = [c for c in state.candidates if not c.is_rejected]
    if candidates:
        lines.append(f"🎯 **今日候选股 {len(candidates)} 只**")
        for c in candidates[:3]:
            lines.append(
                f"  · **{c.name}**（{c.code[:6]}）"
                f"　保守买入 {c.conservative_entry:.2f}　止损 {c.stop_loss:.2f}"
            )
        if len(candidates) > 3:
            lines.append(f"  · ...共 {len(candidates)} 只，完整执行清单见Web")
    else:
        lines.append("🚫 今日无候选股（观察模式）")
    lines.append("")

    # 重点板块
    hot_sectors = [s for s in state.sector_stats if s.phase in ("升温", "主升")][:3]
    if hot_sectors:
        lines.append("🔥 **升温板块：**" + " | ".join(s.industry for s in hot_sectors))

    # Web链接
    if web_url:
        lines.append("")
        lines.append(f"📋 [完整报告]({web_url})")

    return title, "\n".join(lines)


def build_post_market_summary(state: PipelineState, web_url: str = "") -> tuple[str, str]:
    """
    盘后复盘简报（手机推送版）。

    Returns:
        (title, content)
    """
    regime = state.market_regime
    trade_date = _fmt_date(state.trade_date)

    title = f"【盘后】{trade_date} {regime.label} | {_post_market_headline(state)}"

    lines: list[str] = []

    # 今日大盘一句话
    lines.append(f"**今日市场：{regime.label}**")
    lines.append(
        f"📊 涨停{regime.limit_up_count}家 | 跌停{regime.limit_down_count}家 | "
        f"下跌{regime.down_count}家"
    )
    north = regime.north_net_million
    if north is not None:
        sign = "+" if north >= 0 else ""
        lines.append(f"🌊 北向 {sign}{north/10000:.1f}亿")
    lines.append("")

    # 观察池表现回顾
    try:
        from app.strategy.forward_tracker import get_recent_watchlist_perf
        perfs = get_recent_watchlist_perf(state.trade_date, days=5)
        filled = [p for p in perfs if p["pct_return"] is not None]
        if filled:
            lines.append("**📋 观察池近期表现**")
            for p in filled[-3:]:  # 最近3条
                emoji = "✅" if p["pct_return"] >= 0 else "❌"
                stop = " 🛑止损" if p["hit_stop_loss"] else ""
                lines.append(
                    f"  {emoji} {p['name']} {p['pct_return']:+.1f}%{stop}"
                    f"（{p['run_date']}选）"
                )
            lines.append("")
    except Exception:
        pass

    # 明日值得关注的板块方向
    next_hot = [s for s in state.sector_stats if s.phase in ("升温", "主升")][:4]
    if next_hot:
        lines.append("🔭 **明日关注方向：**" + " | ".join(s.industry for s in next_hot))
    lines.append("")

    # 明日候选
    candidates = [c for c in state.candidates if not c.is_rejected]
    if candidates:
        lines.append(f"🎯 **明日候选 {len(candidates)} 只** — 完整执行清单见Web")
    else:
        lines.append("💤 明日暂无候选股，继续观察")

    if web_url:
        lines.append("")
        lines.append(f"📋 [完整复盘报告]({web_url})")

    return title, "\n".join(lines)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _fmt_date(yyyymmdd: str) -> str:
    """20260612 → 06/12"""
    return f"{yyyymmdd[4:6]}/{yyyymmdd[6:]}"


def _position_hint(regime) -> str:
    """根据仓位上限生成一句话操作提示。"""
    limit = regime.position_limit
    if limit == 0:
        return "空仓观察"
    elif limit <= 0.3:
        return f"轻仓{int(limit*100)}%"
    elif limit <= 0.5:
        return f"半仓{int(limit*100)}%"
    else:
        return f"积极{int(limit*100)}%"


def _post_market_headline(state: PipelineState) -> str:
    """盘后标题一句话，反映今日关键信号。"""
    regime = state.market_regime
    # 判断今日情绪方向
    if regime.limit_up_count >= 100 and regime.limit_down_count < 20:
        return "情绪偏强"
    elif regime.limit_down_count >= 50 or regime.down_count > 3000:
        return "情绪偏弱"
    elif regime.north_net_million and regime.north_net_million > 50000:
        return "北向大幅流入"
    else:
        return "震荡整理"
