"""
节点 D: 风控 + 多空辩论（Phase 3）。

执行顺序：
  1. 硬规则风控（一票否决，不经过LLM）
  2. 空头Agent：识别风险点，判断是否否决
  3. 多头Agent：寻找做多理由
  4. 首席风控：硬规则 > 空头否决 > 多头论点
  5. 更新 Candidate.risk_flags，过滤被否决的股票

成本控制：仅对通过硬规则的候选股调用LLM（最多 MAX_CANDIDATES 次 flash 调用）。
"""

import json
import logging
import re
from pathlib import Path

from app.data.composite_provider import CompositeProvider
from app.llm.client import LLMClient
from app.state import Candidate, Debate, PipelineState

logger = logging.getLogger(__name__)

_BULL_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "bull_agent.txt"
_BEAR_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "bear_agent.txt"

# 单票涨幅超过此值视为追高风险（软规则，影响评分不否决）
_HIGH_GAIN_SOFT = 8.0
# 单票涨幅超过此值直接硬否决（可能是利好兑现顶部）
_HIGH_GAIN_HARD = 15.0


def node_risk_debate(state: PipelineState) -> PipelineState:
    """风控 + 多空辩论：过滤被否决候选股，为保留股票附加多空观点。"""
    if not state.candidates:
        logger.info("[节点D] 无候选股，跳过风控辩论")
        return state

    logger.info("[节点D] 开始风控+多空辩论，候选股 %d 只", len(state.candidates))

    # O12: 初始化 provider 供个股新闻查询
    from app.data.composite_provider import CompositeProvider
    provider = CompositeProvider()

    # 获取板块退潮信息（供空头Agent参考）
    decay_industries = {
        s.industry for s in state.sector_stats if s.phase == "退潮"
    }

    passed: list[Candidate] = []
    vetoed: list[Candidate] = []

    for candidate in state.candidates:
        # Step 1: 硬规则（不调用LLM，快速过滤）
        hard_veto, veto_reason = _check_hard_rules(candidate, decay_industries)
        if hard_veto:
            candidate.risk_flags.append(f"❌ 硬否决: {veto_reason}")
            vetoed.append(candidate)
            logger.info("  [否决] %s — %s", candidate.name, veto_reason)
            continue

        # Step 2: LLM多空辩论（含O12实时个股新闻）
        bull_points, bear_points, risk_level, soft_veto, soft_reason = _llm_debate(
            candidate, decay_industries, provider=provider
        )

        candidate.risk_flags.extend(bear_points)
        if candidate.debate is None:
            candidate.debate = Debate()

        # 软否决：LLM明确否决 OR (高风险+板块退潮，双重确认才否决)
        in_decay = candidate.theme in decay_industries
        auto_veto = (risk_level == "high") and in_decay
        final_veto = soft_veto or auto_veto
        final_reason = soft_reason if soft_veto else (
            f"高风险评级+板块退潮({candidate.theme})，自动否决" if auto_veto else ""
        )

        if final_veto:
            candidate.risk_flags.append(f"⚠️ 空头否决: {final_reason}")
            candidate.debate.verdict = "否决"
            candidate.debate.verdict_reason = final_reason
            candidate.debate.bull_points = bull_points
            candidate.debate.bear_points = bear_points
            vetoed.append(candidate)
            logger.info("  [软否决] %s — %s", candidate.name, final_reason)
        else:
            candidate.debate.verdict = "通过"
            candidate.debate.verdict_reason = f"风险等级{risk_level}，多空辩论通过"
            candidate.debate.bull_points = bull_points
            candidate.debate.bear_points = bear_points
            passed.append(candidate)

    logger.info(
        "[节点D] 风控完成: 通过 %d 只 / 否决 %d 只",
        len(passed), len(vetoed),
    )

    # 保留通过的候选股，否决的移入 debate 记录（不在报告主体展示）
    state.candidates = passed
    state.debate = {
        "passed": len(passed),
        "vetoed": len(vetoed),
        "vetoed_names": [c.name for c in vetoed],
    }
    return state


# ──────────────────────────────────────────────
# 硬规则风控（无LLM）
# ──────────────────────────────────────────────

def _check_hard_rules(
    candidate: Candidate,
    decay_industries: set[str],
) -> tuple[bool, str]:
    """
    硬规则一票否决。返回 (is_vetoed, reason)。
    硬规则优先于一切，不受多头论点影响。
    """
    name = candidate.name
    f = candidate.factors

    # 1. ST / *ST 股票
    if "ST" in name or "退" in name:
        return True, f"名称含ST/退：{name}"

    # 2. 当日涨幅超15%（利好兑现顶部风险）
    if f.pct_change >= _HIGH_GAIN_HARD:
        return True, f"当日涨幅{f.pct_change:.1f}%>15%，追高风险极高"

    # 3. 市值过小（流动性风险）
    if f.market_cap < 20:
        return True, f"市值{f.market_cap:.0f}亿<20亿，流动性不足"

    return False, ""


# ──────────────────────────────────────────────
# LLM 多空辩论
# ──────────────────────────────────────────────

def _build_stock_info(candidate: Candidate, decay_industries: set[str], news_summary: str = "") -> str:
    """将候选股数据格式化为 LLM 可读的文本。"""
    f = candidate.factors
    p = candidate.trade_plan
    in_decay = candidate.theme in decay_industries

    # 千股千评得分：0 可能是"数据不可用"而非真实低分，避免 LLM 误判
    comment_str = (
        f"千股千评综合得分：{f.comment_score:.0f}（满分100，>60为热门）"
        if f.comment_score > 0
        else "千股千评：数据暂不可用（不影响本次评估）"
    )

    return (
        f"股票：{candidate.name}（{candidate.code}）｜行业：{candidate.theme}\n"
        f"当日涨跌幅：{f.pct_change:+.2f}%　市值：{f.market_cap:.0f}亿\n"
        f"RPS50（近50日相对强弱）：{f.rps50:.0f}（>70为强势）\n"
        f"主力净流入（3日）：{f.fund_flow_3d/10000:.1f}亿元\n"
        f"换手率：{f.turnover_rate:.1f}%　近5日平均振幅：{f.avg_amplitude_5d:.1f}%\n"
        f"技术信号：{'　'.join(candidate.filters_passed[:4])}\n"
        f"止损价：{p.stop_loss:.2f}　止盈1：{p.take_profit_1:.2f}　止盈2：{p.take_profit_2:.2f}\n"
        f"所属板块状态：{'⚠️ 退潮' if in_decay else '正常'}\n"
        f"龙虎榜：{'是' if f.lhb_flag else '否'}　{comment_str}"
        + (f"\n{news_summary}" if news_summary else "")
    )


def _fetch_stock_news_summary(candidate: Candidate, provider) -> str:
    """
    O12: 获取个股最近新闻摘要，供 Bear Agent 评估实时风险。
    失败时返回空字符串，不阻断主流程。
    """
    try:
        news_df = provider.get_stock_news(candidate.code)
        if news_df is None or news_df.empty:
            return ""

        # 取最近5条新闻标题
        title_col = next(
            (c for c in ["新闻标题", "标题", "title"] if c in news_df.columns), None
        )
        if not title_col:
            return ""

        titles = news_df[title_col].dropna().head(5).tolist()
        if not titles:
            return ""

        return "最近新闻：" + "；".join(str(t)[:40] for t in titles)
    except Exception:
        return ""


def _llm_debate(
    candidate: Candidate,
    decay_industries: set[str],
    provider=None,
) -> tuple[list[str], list[str], str, bool, str]:
    """
    调用 DeepSeek-flash 进行多空辩论。
    O12: 可选传入 provider，获取实时个股新闻加入 Bear Agent 评估。
    返回 (bull_points, bear_points, risk_level, soft_veto, veto_reason)
    失败时返回默认值，不阻断流程。
    """
    # O12: 获取个股实时新闻
    news_summary = _fetch_stock_news_summary(candidate, provider) if provider else ""
    stock_info = _build_stock_info(candidate, decay_industries, news_summary)
    llm = LLMClient()

    bull_points: list[str] = []
    bear_points: list[str] = []
    risk_level = "medium"
    soft_veto = False
    veto_reason = ""

    # ---- 空头Agent ----
    try:
        bear_prompt = _BEAR_PROMPT_PATH.read_text(encoding="utf-8").replace("{stock_info}", stock_info)
        bear_raw = llm.chat(
            [{"role": "user", "content": bear_prompt}],
            task_type="flash",
            temperature=0.2,
            max_tokens=300,
        )
        bear_result = _parse_json_safe(bear_raw)
        if bear_result:
            soft_veto = bool(bear_result.get("hard_veto", False))
            veto_reason = str(bear_result.get("veto_reason", ""))
            bear_points = [str(p) for p in bear_result.get("bear_points", [])]
            risk_level = str(bear_result.get("risk_level", "medium"))
    except Exception as e:
        logger.debug("空头Agent调用失败: %s", e)

    # ---- 多头Agent（空头否决时也跑，提供参考）----
    try:
        bull_prompt = _BULL_PROMPT_PATH.read_text(encoding="utf-8").replace("{stock_info}", stock_info)
        bull_raw = llm.chat(
            [{"role": "user", "content": bull_prompt}],
            task_type="flash",
            temperature=0.2,
            max_tokens=300,
        )
        bull_result = _parse_json_safe(bull_raw)
        if bull_result:
            bull_points = [str(p) for p in bull_result.get("bull_points", [])]
    except Exception as e:
        logger.debug("多头Agent调用失败: %s", e)

    return bull_points, bear_points, risk_level, soft_veto, veto_reason


def _parse_json_safe(raw: str) -> dict:
    """从 LLM 输出安全解析 JSON，容忍 markdown 代码块。"""
    text = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start: end + 1])
    except json.JSONDecodeError:
        return {}
