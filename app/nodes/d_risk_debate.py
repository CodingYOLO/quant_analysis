"""
节点 D: 风控 + 多空辩论。
硬规则风控（ST/退市/商誉/解禁/问询函）→ 多空辩论 → 空头有否决权。
Phase 0 占位，Phase 3 实现。
"""

import logging
from app.state import PipelineState

logger = logging.getLogger(__name__)


def node_risk_debate(state: PipelineState) -> PipelineState:
    """占位：Phase 3 实现风控与多空辩论逻辑。"""
    logger.info("[节点D] 风控+多空辩论（占位）")
    state.debate = {"verdict": "占位", "reason": "Phase 3 实现"}
    return state
