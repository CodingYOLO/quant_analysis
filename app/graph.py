"""
LangGraph 流水线图定义。
节点顺序: A(市场Gate) → B(主题分析) → C(选股) → D(风控辩论) → E(报告)
每个节点都可以独立测试和替换。
"""

from langgraph.graph import StateGraph, END

from app.state import PipelineState
from app.nodes.a_market_gate import node_market_gate
from app.nodes.b_theme_analysis import node_theme_analysis
from app.nodes.c_stock_selection import node_stock_selection
from app.nodes.d_risk_debate import node_risk_debate
from app.nodes.e_report import node_report


def build_graph() -> StateGraph:
    """构建并编译 LangGraph 状态图。"""
    # LangGraph 需要 TypedDict 或 dict 作为 state schema
    # 我们通过 dict 传递 PipelineState 的序列化内容
    graph = StateGraph(dict)

    graph.add_node("market_gate", _wrap(node_market_gate))
    graph.add_node("theme_analysis", _wrap(node_theme_analysis))
    graph.add_node("stock_selection", _wrap(node_stock_selection))
    graph.add_node("risk_debate", _wrap(node_risk_debate))
    graph.add_node("report", _wrap(node_report))

    graph.set_entry_point("market_gate")
    graph.add_edge("market_gate", "theme_analysis")
    graph.add_edge("theme_analysis", "stock_selection")
    graph.add_edge("stock_selection", "risk_debate")
    graph.add_edge("risk_debate", "report")
    graph.add_edge("report", END)

    return graph.compile()


def _wrap(node_fn):
    """
    将接收/返回 PipelineState 的节点函数包装为 LangGraph dict-based 接口。
    LangGraph 传入 dict，节点函数操作 pydantic 模型，再转回 dict。
    """
    def wrapped(state: dict) -> dict:
        pipeline_state = PipelineState(**state)
        result = node_fn(pipeline_state)
        return result.model_dump()
    wrapped.__name__ = node_fn.__name__
    return wrapped
