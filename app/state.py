"""
PipelineState: 流经所有 LangGraph 节点的共享状态对象。
所有节点只读取/写入这个对象，不直接传递参数。
"""

from typing import Any
from pydantic import BaseModel, Field


class MarketRegime(BaseModel):
    """大盘状态（吴川体系第一层输出）。"""
    label: str = ""                    # 强势/震荡/弱势
    can_open: bool = False             # 是否允许开仓
    position_limit: float = 0.0       # 仓位上限 0~1
    limit_up_count: int = 0           # 涨停家数（≥80 为强势信号）
    limit_down_count: int = 0         # 跌停家数（>30 为弱势信号）
    down_count: int = 0               # 全市场下跌家数（>3000 暂停开仓）
    consecutive_limit_high: int = 0   # 连板最高板数（情绪高度）
    pct_above_ma5: float = 0.0        # 全市场站上MA5的股票占比
    pct_above_ma20: float = 0.0       # 全市场站上MA20的股票占比
    hs300_above_ma5: bool = False      # 沪深300是否站上MA5
    hs300_above_ma20: bool = False     # 沪深300是否站上MA20
    north_net_million: float = 0.0    # 北向资金净买入（万元）
    reason: str = ""                   # 判断理由


class Theme(BaseModel):
    """主题/板块。"""
    name: str = ""
    heat: float = 0.0                  # 热度分 0~10
    phase: str = ""                    # 升温/发酵/退潮
    evidence: list[str] = Field(default_factory=list)   # 支撑证据（新闻摘要）
    concept_codes: list[str] = Field(default_factory=list)  # 映射到的akshare概念代码


class StockFactors(BaseModel):
    """候选股因子数据。"""
    close: float = 0.0
    pct_change: float = 0.0
    volume: float = 0.0
    turnover_rate: float = 0.0
    market_cap: float = 0.0           # 流通市值（亿元）
    fund_flow_3d: float = 0.0         # 主力净流入（万元，超大单+大单）
    avg_amplitude_5d: float = 0.0     # 近5日平均振幅（%），短线空间指标
    rps50: float = 0.0                # 近50日相对强弱百分位（0~100）
    pullback_score: float = 0.0       # 缩量回踩质量评分（0~100）
    lhb_flag: bool = False             # 是否上龙虎榜
    north_flow: float = 0.0           # 北向资金净买入（万元）
    comment_score: float = 0.0        # 千股千评综合得分


class TradePlan(BaseModel):
    """个股交易执行计划（吴川体系T+1框架）。"""
    buy_conservative: float = 0.0    # 保守买入价（20日VWAP，主力成本区）
    buy_aggressive: float = 0.0      # 激进买入价（当日收盘价）
    stop_loss: float = 0.0           # 止损价（MA5，跌破无条件止损）
    take_profit_1: float = 0.0       # 止盈1（+5%，减仓一半）
    take_profit_2: float = 0.0       # 止盈2（+8%，继续减仓）
    position_pct: float = 0.0        # 建议单票仓位（强势5%，震荡3%，弱势0%）
    execution_checklist: str = ""    # 次日09:30-09:40盘前观察清单


class Candidate(BaseModel):
    """候选股票。"""
    code: str = ""
    name: str = ""
    theme: str = ""
    factors: StockFactors = Field(default_factory=StockFactors)
    filters_passed: list[str] = Field(default_factory=list)
    filters_failed: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    trend_summary: str = ""           # 近10日走势摘要（量价形态分析）
    trade_plan: TradePlan = Field(default_factory=TradePlan)  # 交易执行计划


class SectorStat(BaseModel):
    """板块热度量化统计（Phase 2 纯量化输出）。"""
    industry: str = ""                      # 行业名称
    stock_count: int = 0                    # 板块内股票数量
    flow_5d_100m: float = 0.0              # 5日资金净流入（亿元）
    flow_3d_100m: float = 0.0              # 3日资金净流入（亿元）
    pct_above_ma20: float = 0.0            # 板块内站上MA20的股票比例
    limit_up_count: int = 0                # 板块内当日涨停家数
    consecutive_limit_high: int = 0        # 板块内最高连板高度
    heat_score: float = 0.0                # 综合热度分 0~100
    heat_score_delta_3d: float = 0.0       # 3日热度变化量（正=加速，负=退热）
    pop_concentration: float = 0.0         # 人气集中度（前3股成交额/板块总额，高=拥挤）
    nextday_risk_penalty: float = 0.0      # 次日风险惩罚分 0~100（越高越危险）
    decision: str = ""                      # 分层决策：buy/watch/avoid
    decision_score: int = 0                 # 决策评分 0~100
    phase: str = ""                         # 升温/趋势/退潮/中性
    signal: str = ""                        # 信号描述


class Debate(BaseModel):
    """多空辩论结果。"""
    bull_points: list[str] = Field(default_factory=list)
    bear_points: list[str] = Field(default_factory=list)
    verdict: str = ""                  # 通过/否决
    verdict_reason: str = ""


class RunMeta(BaseModel):
    """本次运行元信息。"""
    elapsed_seconds: float = 0.0
    total_tokens: int = 0
    estimated_cost_cny: float = 0.0
    data_source_status: dict[str, str] = Field(default_factory=dict)  # 接口 -> ok/error
    errors: list[str] = Field(default_factory=list)


class PipelineState(BaseModel):
    """流经所有节点的共享状态，每个节点返回更新后的副本。"""
    trade_date: str = ""               # YYYYMMDD
    market_regime: MarketRegime = Field(default_factory=MarketRegime)
    themes: list[Theme] = Field(default_factory=list)
    sector_stats: list[SectorStat] = Field(default_factory=list)   # Phase 2 板块热度
    candidates: list[Candidate] = Field(default_factory=list)
    debate: dict[str, Any] = Field(default_factory=dict)
    report_md: str = ""
    meta: RunMeta = Field(default_factory=RunMeta)
