"""
暗流低吸榜：把概念资金分成 真暗流 / 量价启动 / 假暗流(在撤) —— 借鉴稳智框架，
但**全部复用同花顺官方口径**（build_concept_persistent_flow · moneyflow_cnt_ths · DDE），
每个数都能在同花顺 APP 逐一核对。绝不另算资金（数据准确性第一）。

分档（纯客观·阈值可调）：
  🟢 真暗流   = 近3日净流入>0 且 今日还在进 且 价没涨(ret5<3%)   —— 持续低位吸筹
  🔵 量价启动 = 近3日净流入>0 且 今日在进 且 价开始动(3%~8%)     —— 暗流转明流
  🔴 假暗流   = 近5日>0 但 近3日≤0 或今日流出                   —— 之前进的钱在撤（别被5日数字骗，稳智精华）
  （已涨 ret5>8% 与 普通/流出 不进榜，仅计数）

诚实：资金=同花顺概念 DDE（估算·非龙虎榜真钱）；分档是客观结构描述·非预测/非荐股。
"""

from __future__ import annotations

import logging

from app.data.composite_provider import CompositeProvider
from app.strategy.concept_flow import build_concept_persistent_flow

logger = logging.getLogger(__name__)

_RET_UP = 8.0        # 已涨阈（近5日涨幅%·超过视为已涨·非低吸）
_RET_QUIET = 3.0     # 价没涨阈（真暗流要求 ret5 < 此·含下跌）
_MIN_MEMBERS = 5     # 概念成分下限
_SLIM_KEYS = ("concept", "cum3", "cum5", "today_net", "ret5", "pen5", "consec_days", "n", "lead")


def build_ambush_board(date: str, provider: CompositeProvider | None = None) -> dict:
    """构建暗流低吸榜。复用概念持续流入榜（同花顺官方）·只加分档·剔宽概念。"""
    prov = provider or CompositeProvider()
    rows = build_concept_persistent_flow(date, window=5, provider=prov)["rows"]
    buckets: dict[str, list] = {"real": [], "starting": [], "fake": []}
    n_risen = n_out = 0
    for r in rows:
        if r.get("broad") or (r.get("n") or 0) < _MIN_MEMBERS:   # 暗流看 sharp 赛道·剔宽概念/太小概念
            continue
        g = _grade(r)
        if g in buckets:
            buckets[g].append({**{k: r.get(k) for k in _SLIM_KEYS}, "grade": g})
        elif g == "risen":
            n_risen += 1
        else:
            n_out += 1
    for k in buckets:
        buckets[k].sort(key=lambda x: (x.get("cum3") if x.get("cum3") is not None else -1e9), reverse=True)
    return {
        "date": date,
        "real": buckets["real"], "starting": buckets["starting"], "fake": buckets["fake"],
        "n_risen": n_risen, "n_outflow": n_out,
        "note": ("暗流分档全部基于同花顺官方概念资金(moneyflow_cnt_ths·DDE估算)·可在同花顺 APP 逐一核对。"
                 "🟢真暗流=近3日净流入+今日在进+价没涨(低位吸筹) · 🔵量价启动=资金进+价开始动 · "
                 "🔴假暗流=5日看着流入但近3日/今日在撤(别被5日数字骗)。"
                 "资金为估算·非龙虎榜真钱；分档为客观结构描述·非预测非荐股。"),
    }


def _grade(r: dict) -> str:
    """单概念暗流分档（纯客观·同花顺官方多窗口）。real/starting 要求近3日与今日都在进（真持续）。"""
    cum3 = r.get("cum3") or 0.0
    cum5 = r.get("cum5") or 0.0
    today = r.get("today_net") or 0.0
    ret5 = r.get("ret5")
    ret5 = ret5 if ret5 is not None else 0.0
    if ret5 > _RET_UP:
        return "risen"
    if cum3 > 0 and today > 0:                          # 近3日+今日都净流入=真持续进
        return "real" if ret5 < _RET_QUIET else "starting"
    if cum5 > 0 and (cum3 <= 0 or today < 0):           # 5日看着流入但3日转负/今日流出=在撤
        return "fake"
    return "outflow"
