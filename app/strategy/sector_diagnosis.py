"""
板块诊断：价格-资金背离 + 多周期宽度 + 状态机（搬吴川框架·但每个状态用回测验证）。

灵魂：**不硬编码任何未经验证的阈值**——所有状态阈值放 `CONFIG`，由 `sector_state_eval` 在历史上
标定/验证；没有预测力的状态标"仅参考"或不展示（诚实优先）。

口径与全站一致（复用 industry_flow._industry_agg + breadth_qfq）：
  - 资金 = Tushare 官方 moneyflow 主力净流入(超大单+大单·估算·非龙虎榜真机构钱)，逐日按申万二级聚合。
  - F1d = 当日板块净流入的**横截面标准化强度**(z-score·让不同板块可比)；F3d/F5d = 近3/5日 F1d 滚动和。
  - 资金加速度 accel = F1d_t − F1d_{t-1}（"资金减速/加速"的量化·背离核心输入）。
  - 多周期宽度 = 板块内成分股站上 MA5/10/20/60 前复权均线的占比%（板块内部健康度）。

point-in-time：每个交易日的指标只用 ≤ 当日 数据；涨跌停/停牌由底层日线口径与聚合中位数抵消极端值。
现象描述·非买卖建议（[[no-directional-recommendations]]）。
"""

from __future__ import annotations

import logging

import numpy as np

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

# ── 状态阈值（初始为合理先验·**待回测标定**·非吴川拍脑袋数字）────────────────────────
CONFIG = {
    "ma5_high": 65.0,        # MA5 宽度"高位"(%)：短期普涨
    "ma5_low": 20.0,         # MA5 宽度"极低"(%)：洗盘谷底候选
    "ma60_support": 55.0,    # MA60 宽度"仍撑"(%)：中期趋势未破
    "ret5_up": 2.0,          # 近5日板块涨幅"价涨"阈值(%)
    "ret5_flat": 2.0,        # 近5日板块涨幅"价平/没涨"上限(%)——暗流用
    "f3d_pos": 0.5,          # F3d"资金持续为正"阈值(标准化)
}

_MA_WINDOWS = (5, 10, 20, 60)


def classify_state(m: dict, cfg: dict = CONFIG) -> str:
    """把单板块单日指标组合成命名状态（纯函数·可单测·规则参数全走 cfg）。

    m 需含：ma5,ma20,ma60(宽度%)、ma5_prev(几日前MA5宽度)、f1d,f1d_prev、f3d,f3d_prev、ret5。
    None 值视为缺失→保守归为"中性"。优先级从风险到机会。
    """
    ma5, ma20, ma60 = m.get("ma5"), m.get("ma20"), m.get("ma60")
    f1d, f1d_prev = m.get("f1d"), m.get("f1d_prev")
    f3d, f3d_prev = m.get("f3d"), m.get("f3d_prev")
    ret5, ma5_prev = m.get("ret5"), m.get("ma5_prev")
    if ma5 is None or f1d is None or f3d is None or ret5 is None:
        return "中性"

    # 资金加速度：优先用显式传入的稳定分母(pen)差分；缺省才回退 f1d 差分(向后兼容旧测试)
    accel = m.get("accel")
    if accel is None:
        accel = (f1d - f1d_prev) if f1d_prev is not None else 0.0

    # 1) 顶背离/派发：价涨 + 宽度高位 + 资金减速（价量背离·散场前最后的舞蹈）
    if ret5 > cfg["ret5_up"] and ma5 >= cfg["ma5_high"] and accel < 0 and \
            (f3d_prev is None or f3d < f3d_prev):
        return "顶背离"

    # 2) 暗流/等点火：价平或跌 + F3d 持续为正且不减（资金逆价流入·埋伏）
    if ret5 <= cfg["ret5_flat"] and f3d > cfg["f3d_pos"] and \
            (f3d_prev is None or f3d >= f3d_prev):
        return "暗流"

    # 3) 高位回调：MA5 宽度从高位破位、但 MA60 宽度仍在高位（短破长撑·未崩）
    if ma5_prev is not None and ma60 is not None and \
            ma5_prev >= cfg["ma5_high"] and ma5 < cfg["ma5_high"] and ma60 >= cfg["ma60_support"]:
        return "高位回调"

    # 4) 洗盘谷底：MA5 宽度极低 + 资金流出减速（F1d 由深负收窄回升）
    if ma5 <= cfg["ma5_low"] and f1d_prev is not None and f1d > f1d_prev and f1d_prev < 0:
        return "洗盘谷底"

    # 5) 健康上行：宽度高位 + 价涨 + 资金仍进
    if ma5 >= cfg["ma5_high"] and ret5 > 0 and f3d > 0:
        return "健康上行"

    return "中性"


STATES = ("顶背离", "暗流", "高位回调", "洗盘谷底", "健康上行", "中性")


# ── 从特征序列取当日状态（canonical·回测与面板共用同一逻辑，确保展示=已验证）──────────
def state_at(s: dict, i: int, denom: str = "pen", prev_gap: int = 3) -> str:
    """sector_metrics.build_features 的单板块序列 s 第 i 天 → 状态。

    denom 喂水平/趋势（{denom}_z / {denom}_f3d）；**加速度恒用 pen_accel（稳定分母）**。
    """
    def at(key, idx):
        arr = s.get(key, [])
        return arr[idx] if 0 <= idx < len(arr) and arr[idx] is not None else None
    m = {
        "ma5": at("ma5", i), "ma20": at("ma20", i), "ma60": at("ma60", i),
        "ma5_prev": at("ma5", i - prev_gap),
        "f1d": at(f"{denom}_z", i), "f1d_prev": at(f"{denom}_z", i - 1),
        "f3d": at(f"{denom}_f3d", i), "f3d_prev": at(f"{denom}_f3d", i - 1),
        "accel": at("pen_accel", i),
        "ret5": _ret5(s.get("pct", []), i),
    }
    return classify_state(m)


# ── 回测验证结论（post-924 L2 · 924前后双期交叉 · 95%CI）→ 决定可信度分层与展示 ─────────
# 只有跨 regime 稳健的状态才作信号；其余仅描述，避免用未验证判断误导用户。
STATE_VERDICT = {
    "顶背离": {"tier": "signal", "dir": "bearish", "label": "见顶/减仓·避雷", "sub": "非买点",
              "post": "T+5胜36.6%(基准45.2%)·超额CI[-0.72,-0.47]显著负",
              "pre": "924前 超额CI[-0.31,-0.08] 仍显著负·方向一致",
              "caveat": "跨regime稳健·但小edge→只作避雷/择时辅助·非alpha来源"},
    "洗盘谷底": {"tier": "reference", "dir": "weak_pos", "label": "超跌·仅参考", "sub": "比基准少亏·非买点",
               "post": "T+5胜49.2%·超额CI[0.01,0.23]显著·但绝对均值仍-0.37%",
               "pre": "924前 超额CI[0.26,0.44] 仍显著正·方向一致",
               "caveat": "非买点(绝对收益仍负)·需配合资金加速度转正才可能升级·绝不重仓"},
    "暗流": {"tier": "descriptive", "dir": "none", "label": "资金逆价流入", "sub": "未过回测·仅描述",
            "post": "pen 超额CI跨0·触发少", "pre": "方向不一致",
            "caveat": "需换 press 分母 + 点火前置条件重测(待做)"},
    "高位回调": {"tier": "descriptive", "dir": "none", "label": "短破长撑", "sub": "未过回测·仅描述",
              "post": "超额CI跨0(中性)", "pre": "924前显著负·方向不一致", "caveat": "不作信号"},
    "健康上行": {"tier": "descriptive", "dir": "none", "label": "宽度高位·价涨·资金进", "sub": "未过回测·仅描述",
              "post": "post-924显著负(追高被套)", "pre": "924前不显著·疑过拟合本轮", "caveat": "不作信号"},
    "中性": {"tier": "descriptive", "dir": "none", "label": "无明显形态", "sub": "",
            "post": "", "pre": "", "caveat": ""},
}

_TIER_ORDER = {"signal": 0, "reference": 1, "descriptive": 2}


def build_diagnosis(end: str, provider: CompositeProvider | None = None, level: str = "L2",
                    window: int = 14, top: int = 24, min_n: int = 5, force: bool = False) -> dict:
    """当日板块诊断面板：各板块当前状态(带回测可信度分层) + 关键指标 + 大类资金地图。

    T日盘后出诊断·供 T+1 参考(资金为盘后数据)。现象描述·非买卖建议。精选活跃(信号态优先)。
    min_n: 最少成分股(剔除微型板块噪音·如林业Ⅱ)。按日缓存(供 warmup 预热·打开秒显示)。
    """
    import json

    from app.data.cache import _cache_path
    from app.factors.breadth_qfq import _recent_trade_dates
    from app.strategy.sector_attribution import build_flow_map
    from app.strategy.sector_metrics import build_features
    provider = provider or CompositeProvider()
    cache = _cache_path("sector_diagnosis", f"{end}_{level}").with_suffix(".json")
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text("utf-8"))
        except Exception:
            pass

    dates = _recent_trade_dates(provider, end, window + 8)
    feats = build_features(end, dates[0], provider, level=level)
    fdates, sectors = feats["dates"], feats["sectors"]
    if not fdates:
        raise ValueError(f"{end} 无诊断数据")
    i = len(fdates) - 1

    rows = []
    for nm, s in sectors.items():
        n = _last(s, "n", i)
        if not n or n < min_n:                                     # 剔微型板块噪音
            continue
        st = state_at(s, i, "pen")
        v = STATE_VERDICT.get(st, STATE_VERDICT["中性"])
        rows.append({
            "sector": nm, "state": st, "tier": v["tier"], "dir": v["dir"],
            "label": v["label"], "sub": v["sub"], "caveat": v["caveat"],
            "post": v["post"], "pre": v["pre"],
            "ma5": _last(s, "ma5", i), "ma20": _last(s, "ma20", i), "ma60": _last(s, "ma60", i),
            "pen": _last(s, "pen", i), "pen_z": _last(s, "pen_z", i),
            "pen_accel": _last(s, "pen_accel", i), "pen_f3d": _last(s, "pen_f3d", i),
            "ret5": _ret5(s.get("pct", []), i), "net": _last(s, "net", i), "n": _last(s, "n", i),
        })
    # 精选活跃：信号态优先，其余按活跃度(|近5日涨幅| + |资金z|)降序
    rows.sort(key=lambda r: (_TIER_ORDER.get(r["tier"], 3),
                             -(abs(r["ret5"] or 0) + abs((r["pen_z"] or 0) * 1.5))))
    result = {
        "end": end, "date": fdates[-1], "level": level, "n_total": len(rows),
        "flow_map": build_flow_map(end, window=15, provider=provider),
        "sectors": rows[:top] if top else rows,
        "verdict": STATE_VERDICT,
        "note": ("T日盘后出诊断·供 T+1 参考（资金为盘后数据·杜绝当日盘中用）。现象描述·非买卖建议。"
                 "仅『顶背离』经924前后双期回测(避雷用·非买点)·『洗盘谷底』仅参考·其余未过验证仅描述。"),
    }
    if rows and fdates[-1] == end:                                  # 冻结防护：数据日完整(=end)才落缓存
        try:
            cache.write_text(json.dumps(result, ensure_ascii=False), "utf-8")
        except Exception as e:
            logger.debug("[诊断] 缓存写入失败: %s", e)
    return result


def _last(s: dict, key: str, i: int):
    arr = s.get(key, [])
    return arr[i] if 0 <= i < len(arr) else None


def _zscore_cross(net_map: dict) -> dict:
    """某日全市场板块净流入 → 横截面 z-score（稳健：用中位数/MAD·抗离群）。空→{}。"""
    if not net_map:
        return {}
    vals = np.array(list(net_map.values()), dtype=float)
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med))) or (float(vals.std()) or 1.0)
    scale = mad * 1.4826 or 1.0                                       # MAD→σ 一致估计
    return {k: round((v - med) / scale, 2) for k, v in net_map.items()}


def _rollsum(seq: list, end_idx: int, n: int) -> float | None:
    """seq[end_idx-n+1 .. end_idx] 的非空求和（滚动 F3d/F5d）。全空→None。"""
    lo = max(0, end_idx - n + 1)
    vals = [v for v in seq[lo:end_idx + 1] if v is not None]
    return round(sum(vals), 2) if vals else None


def _ret5(pct: list, i: int) -> float | None:
    """第 i 天的近5日板块涨幅(复利·中位口径)。"""
    lo = max(0, i - 4)
    vals = [p for p in pct[lo:i + 1] if p is not None]
    if not vals:
        return None
    prod = 1.0
    for p in vals:
        prod *= (1 + p / 100.0)
    return round((prod - 1) * 100, 2)
