"""资金三角：把"主力估算"升级为多源印证的真机构钱信号。

A 股个股北向持股自 2024-08 起被交易所停止披露，已无法获取个股级北向。
本模块改用真实可得的三路口径交叉印证：
  1. 主力资金 —— moneyflow（逐笔分档估算·代理·全市场覆盖）
  2. 龙虎榜机构 —— top_inst 机构专用席位净买（真金白银·上榜才有·稀疏但高信号）
  3. 大盘北向 —— moneyflow_hsgt 当日净额（真钱·仅大盘级·作环境背景，非个股腿）

核心价值：把"只有估算"升级为"真钱印证/背离警示"——
  · 估算净流入 + 龙虎榜机构真买  → 真钱印证（高置信）
  · 估算净流入 + 龙虎榜机构净卖  → 背离警示（估算可能失真）

设计：纯分类函数 `_classify` 与数据取数解耦，便于零网络单测；
批量按交易日拉一次 top_inst 供所有个股复用，主力资金由调用方注入避免重复取数。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.factors.breadth_qfq import _recent_trade_dates

# —— 配置化阈值（避免硬编码·可后续按回测校准）——
_LOOKBACK_DAYS = 5      # 龙虎榜机构净买回看窗口（上榜稀疏·比主力3日略长以提高命中）
_FLOW_EPS_YI = 0.2      # 主力净流入"近似零"带宽（亿元）·消噪
_INST_EPS_YI = 0.05     # 机构净买"近似零"带宽（亿元）

# —— 三源一致性打分权重（配置化·可后续回测校准）——
# 打的是『主力估算/龙虎榜机构真钱/北向 三路方向是否一致』,不预测涨跌、不等于胜率。
_SCORE_BASE = 50        # 中性基线
_W_INST_BUY = 25        # 龙虎榜机构真买基础分（真钱·权重最高·唯一能触发"共振"档）
_W_INST_DAY = 3         # 每多一日机构净买的持续性加成（持续吸筹置信更高）
_INST_DAY_CAP = 3       # 持续性加成封顶天数
_W_INST_SELL = 30       # 龙虎榜机构净卖扣分（真钱在撤·强负面）
_W_MAIN = 15            # 主力估算方向分（代理口径·中等权重）
_W_DIVERGE_PEN = 10     # 主力流入但机构净卖的额外背离惩罚
_W_NORTH = 3            # 北向大盘环境微调
_TH_RESONANCE = 75      # ≥ 三源共振·偏多
_TH_BULLISH = 58        # ≥ 资金偏多
_TH_BEARISH = 35        # ≤ 资金偏空/背离

# —— 三角共振标签 ——
_L_CONFIRM = "真钱印证"
_L_DIVERGE = "背离警示"
_L_NO_TRACE = "机构无足迹"
_L_OUTFLOW = "资金流出"
_L_NEUTRAL = "资金中性"


@dataclass
class FundTriangle:
    """单只个股的资金三角结果（透明展示用，字段单位均为亿元）。"""

    ts_code: str
    main_flow_yi: float      # 主力估算·近3日净流入（代理口径·全覆盖）
    inst_net_yi: float       # 龙虎榜机构·近N日净买（真钱·上榜才有）
    on_lhb: bool             # 近N日是否有机构席位足迹
    north_market_yi: float   # 大盘北向当日净额（环境背景·非个股）
    label: str               # 共振标签：真钱印证/背离警示/机构无足迹/资金流出/资金中性
    level: str               # strong / warn / neutral / weak
    detail: str              # 一句话透明说明（可直接展示）
    inst_buy_days: int = 0       # 近N日机构净买天数（持续吸筹·真钱持续性·上榜稀疏故常为0~3）
    consistency: int = 50        # 三源一致性打分0-100（信号方向一致度·非涨跌预测）
    consistency_label: str = ""  # 一致性标签：三源共振·偏多 / 资金偏多 / 资金中性 / 资金偏空·背离

    def to_dict(self) -> dict:
        return asdict(self)


def _classify(main_flow_yi: float, inst_net_yi: float, on_lhb: bool) -> tuple[str, str, str]:
    """三路共振分类（纯函数·零依赖·可单测）。

    Args:
        main_flow_yi: 主力估算近3日净流入（亿元，正=流入）。
        inst_net_yi: 龙虎榜机构近N日净买（亿元，正=净买）。
        on_lhb: 近N日是否有机构席位足迹（无足迹时 inst_net_yi 无意义）。

    Returns:
        (label, level, detail) 三元组。
    """
    mf = float(main_flow_yi or 0.0)
    inst = float(inst_net_yi or 0.0)

    if mf < -_FLOW_EPS_YI:
        return _L_OUTFLOW, "weak", f"主力近3日净流出 {abs(mf):.2f} 亿·资金在撤"

    if mf > _FLOW_EPS_YI:
        if not on_lhb:
            return (_L_NO_TRACE, "neutral",
                    f"主力估算净流入 {mf:.2f} 亿，但近期龙虎榜无机构席位足迹·仅代理口径无真钱印证")
        if inst > _INST_EPS_YI:
            return (_L_CONFIRM, "strong",
                    f"主力估算 +{mf:.2f} 亿 且 龙虎榜机构真买 +{inst:.2f} 亿·真钱印证")
        if inst < -_INST_EPS_YI:
            return (_L_DIVERGE, "warn",
                    f"主力估算 +{mf:.2f} 亿 但 龙虎榜机构净卖 {abs(inst):.2f} 亿·背离警示")
        return _L_NEUTRAL, "neutral", f"主力估算 +{mf:.2f} 亿，龙虎榜机构席位基本持平"

    # 主力近似零：机构真买仍是有效正信号
    if on_lhb and inst > _INST_EPS_YI:
        return _L_CONFIRM, "strong", f"主力持平，但龙虎榜机构真买 +{inst:.2f} 亿"
    return _L_NEUTRAL, "neutral", "资金无明显方向"


def _consistency(
    main_flow_yi: float,
    inst_net_yi: float,
    on_lhb: bool,
    north_market_yi: float,
    inst_buy_days: int = 0,
) -> tuple[int, str]:
    """三源一致性打分(0-100) + 标签（纯函数·零依赖·可单测）。

    以龙虎榜机构『真钱』为锚、主力估算为辅、北向为环境微调。打的是『三路资金信号
    方向是否一致』的一致度，**不预测涨跌、不等于胜率**。只有出现机构真买（真钱印证）
    才可能进入"三源共振"档——无真钱印证时分数封在偏多以下，诚实反映置信边界。

    Args:
        main_flow_yi: 主力估算近3日净流入（亿元）。
        inst_net_yi: 龙虎榜机构近N日净买（亿元）。
        on_lhb: 近N日是否有机构席位足迹。
        north_market_yi: 大盘北向当日净额（亿元·环境）。
        inst_buy_days: 近N日机构净买天数（持续性加成）。

    Returns:
        (score, label) —— score 为 0~100 整数，label 为四档中文标签。
    """
    mf = float(main_flow_yi or 0.0)
    inst = float(inst_net_yi or 0.0)
    score = float(_SCORE_BASE)

    if on_lhb:                                          # 龙虎榜机构真钱——锚
        if inst > _INST_EPS_YI:
            score += _W_INST_BUY + min(int(inst_buy_days), _INST_DAY_CAP) * _W_INST_DAY
        elif inst < -_INST_EPS_YI:
            score -= _W_INST_SELL
    if mf > _FLOW_EPS_YI:                                # 主力估算——代理
        score += _W_MAIN
    elif mf < -_FLOW_EPS_YI:
        score -= _W_MAIN
    if on_lhb and mf > _FLOW_EPS_YI and inst < -_INST_EPS_YI:   # 真钱与估算背离·额外惩罚
        score -= _W_DIVERGE_PEN
    if north_market_yi > 0:                              # 北向环境微调
        score += _W_NORTH
    elif north_market_yi < 0:
        score -= _W_NORTH

    s = max(0, min(100, int(round(score))))
    if s >= _TH_RESONANCE:
        label = "三源共振·偏多"
    elif s >= _TH_BULLISH:
        label = "资金偏多"
    elif s <= _TH_BEARISH:
        label = "资金偏空·背离"
    else:
        label = "资金中性"
    return s, label


def _inst_net_map(provider: CompositeProvider, dates: list[str]) -> dict[str, tuple[float, int]]:
    """近 N 日龙虎榜机构专用席位净买，{ts_code: (净买合计亿元, 净买天数)}。

    按交易日各拉一次 top_inst（全市场），只取 `机构专用` 席位。先按日聚合（同日多席位合并），
    再跨日累加净额、并统计『当日机构净买为正』的天数（持续吸筹的真钱持续性代理）。
    `机构专用` 为匿名汇总席位，无法追踪同一具体席位，故用"净买天数"而非"同一席位连续"。
    """
    net: dict[str, float] = {}
    days: dict[str, int] = {}
    for d in dates:
        try:
            df = provider.get_lhb_inst(d)
        except Exception:
            continue
        if df is None or df.empty or "exalter" not in df.columns or "net_buy" not in df.columns:
            continue
        inst = df[df["exalter"] == "机构专用"]
        if inst.empty:
            continue
        vals = pd.to_numeric(inst["net_buy"], errors="coerce")
        day_sum: dict[str, float] = {}                     # 当日各股机构净买合计（亿）
        for ts, v in zip(inst["ts_code"], vals):
            if pd.notna(v):
                day_sum[ts] = day_sum.get(ts, 0.0) + float(v) / 1e8   # 元 → 亿
        for ts, s in day_sum.items():
            net[ts] = net.get(ts, 0.0) + s
            if s > _INST_EPS_YI:                            # 当日净买为正 → 计一个吸筹日
                days[ts] = days.get(ts, 0) + 1
    return {ts: (net[ts], days.get(ts, 0)) for ts in net}


def _north_market_yi(provider: CompositeProvider, trade_date: str) -> float:
    """大盘北向当日净额（亿元）·环境背景。取数失败返回 0.0。"""
    try:
        nf = provider.get_north_flow(trade_date)
    except Exception:
        return 0.0
    if nf is None or nf.empty or "north_money" not in nf.columns:
        return 0.0
    v = pd.to_numeric(nf["north_money"], errors="coerce").iloc[0]
    return round(float(v) / 1e4, 2) if pd.notna(v) else 0.0    # 万元 → 亿


def build_fund_triangle(
    provider: CompositeProvider,
    trade_date: str,
    main_flow_map: dict[str, float],
    ts_codes: list[str] | None = None,
    lookback: int = _LOOKBACK_DAYS,
) -> dict[str, FundTriangle]:
    """为给定个股批量构建资金三角。

    Args:
        provider: 数据访问（依赖注入·CompositeProvider 抽象，便于单测打桩）。
        trade_date: 交易日 YYYYMMDD。
        main_flow_map: {ts_code: 主力近3日净流入(亿)}——由调用方复用 signals 已算结果注入，
            避免重复拉 moneyflow（DRY + 省取数）。
        ts_codes: 限定个股；None = 取 main_flow_map 全部键。
        lookback: 龙虎榜机构净买回看交易日数。

    Returns:
        {ts_code: FundTriangle}
    """
    codes = list(ts_codes) if ts_codes is not None else list(main_flow_map.keys())
    try:
        dates = _recent_trade_dates(provider, trade_date, lookback)
    except Exception:
        dates = [trade_date]
    inst_map = _inst_net_map(provider, dates)
    north = _north_market_yi(provider, trade_date)

    out: dict[str, FundTriangle] = {}
    for ts in codes:
        mf = float(main_flow_map.get(ts, 0.0))
        on_lhb = ts in inst_map
        inst_val, buy_days = inst_map.get(ts, (0.0, 0))
        label, level, detail = _classify(mf, inst_val, on_lhb)
        score, score_label = _consistency(mf, inst_val, on_lhb, north, buy_days)
        out[ts] = FundTriangle(
            ts_code=ts,
            main_flow_yi=round(mf, 2),
            inst_net_yi=round(float(inst_val), 2),
            on_lhb=on_lhb,
            north_market_yi=north,
            label=label,
            level=level,
            detail=detail,
            inst_buy_days=int(buy_days),
            consistency=score,
            consistency_label=score_label,
        )
    return out
