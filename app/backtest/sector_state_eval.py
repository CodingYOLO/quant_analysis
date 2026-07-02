"""
板块状态·事件研究回测（诚实验证·924新约束）。

严格性（对齐用户方法论）：
  - **真时点成分**（sector_metrics 用 members_asof·消除成分漂移偏差；残余=退市股不在库·有界往后收敛）。
  - **无未来函数**：资金=T日盘后；入场=**T+1收盘**（严格因果·杜绝用T日盘后数据在T日成交）。
  - **924新约束**：阈值标定/验证只用 2024-09-24 后数据（政策底后风格切换·旧市场规律不污染）。
  - **样本量诚实**：每状态报 胜率+样本量+**bootstrap置信区间**；单状态触发 <30 标"样本不足·仅参考"，不得上线。
  - **924前后交叉检验**：同一状态机在 924 前历史跑一遍（不迁移阈值·只看方向是否同为正期望）；
    前后一致→信心高，仅924后成立→警惕过拟合本轮行情。
  - **双分母分状态**：pen(慢稳)/press(快敏) 分别喂状态机·比分离度 + 时序稳定性。
  - 粒度 L1/L2/L3 各跑一遍对比。

口径：板块收益=成分中位涨幅复利（近似·非可交易指数）。上线标准不因窗口短放松：9个月最多"初步有效·待更多样本"。
"""

from __future__ import annotations

import logging

import numpy as np

from app.strategy.sector_diagnosis import STATES, state_at
from app.strategy.sector_metrics import build_features

logger = logging.getLogger(__name__)

_HORIZONS = (1, 3, 5, 10, 20)          # 持有天数(从 T+1 收盘入场起)
_MIN_SHIP = 30                          # 单状态触发<此=样本不足·仅参考·不得上线
PREV_GAP = 3                            # 宽度"前值"回看(判高位破位)


def evaluate(end: str, start: str = "20240924", level: str = "L2", denom: str = "pen",
             min_members: int = 5, n_boot: int = 1000, seed: int = 42, feats: dict | None = None) -> dict:
    """回测 [start,end] 各板块每日状态的 T+1入场·持有N日前向收益（真时点·bootstrap CI）。

    denom: 喂状态机的资金分母 'pen'(渗透率·慢稳) 或 'press'(压力占比·快敏)。加速度恒用 pen。
    feats: 预构建特征(build_features 输出)·供 pen/press 复用同一次构建·省算力；None 则内部构建。
    """
    rng = np.random.default_rng(seed)
    feats = feats or build_features(end, start, level=level)
    dates, sectors = feats["dates"], feats["sectors"]
    nd = len(dates)
    sig_range = range(PREV_GAP + 2, nd - (max(_HORIZONS) + 2))     # 留状态回看 + 前向

    buckets = {st: {h: [] for h in _HORIZONS} for st in STATES}
    base = {h: [] for h in _HORIZONS}
    counts = {st: 0 for st in STATES}
    state_series: dict[str, list] = {st: [] for st in STATES}      # 状态时序(算稳定性)

    for _nm, s in sectors.items():
        ns = [x for x in s.get("n", []) if x]
        if not ns or np.median(ns) < min_members:                 # 过滤小板块噪音
            continue
        pct = s.get("pct", [])
        prev_state = None
        flips = 0
        n_sig = 0
        for i in sig_range:
            if _fwd(pct, i, 5) is None:
                continue
            st = state_at(s, i, denom)
            counts[st] += 1
            if prev_state is not None and st != prev_state:
                flips += 1
            prev_state = st
            n_sig += 1
            for h in _HORIZONS:
                v = _fwd(pct, i, h)
                if v is not None:
                    buckets[st][h].append(v)
                    base[h].append(v)
        if n_sig:
            state_series.setdefault("_flip", []).append(flips / n_sig)   # 该板块状态翻转率

    base_agg = {h: _agg(base[h], rng, n_boot) for h in _HORIZONS}
    flip = state_series.get("_flip", [])
    out_states = {}
    for st in STATES:
        n = counts[st]
        exc_mean, exc_win = _excess_ci(buckets[st][5], base[5], rng, n_boot)   # 超额(vs基准)95%CI
        out_states[st] = {
            "n_signals": n,
            "shippable": n >= _MIN_SHIP,
            "note": ("" if n >= _MIN_SHIP else "样本不足·仅参考·不得上线"),
            "fwd": {f"t{h}": _agg(buckets[st][h], rng, n_boot) for h in _HORIZONS},
            "excess_t5": _excess(buckets[st][5], base_agg[5]),
            "excess_mean_ci_t5": exc_mean,      # 超额均值95%CI·跨0=不显著
            "excess_win_ci_t5": exc_win,        # 超额胜率(pp)95%CI·跨0=不显著
            "sig_t5": (exc_mean is not None and (exc_mean[0] > 0 or exc_mean[1] < 0)),  # 均值超额显著
        }
    return {
        "ok": True, "end": end, "start": start, "level": level, "denom": denom,
        "span": [dates[sig_range.start], dates[sig_range.stop - 1]] if len(sig_range) else [start, end],
        "n_signal_days": len(sig_range),
        "state_flip_rate": round(float(np.mean(flip)), 3) if flip else None,   # 越低=状态越稳(不抖)
        "baseline": {f"t{h}": base_agg[h] for h in _HORIZONS},
        "states": out_states,
        "note": ("真时点成分·入场=T+1收盘·持有N日·板块中位成分复利(近似)。胜率带bootstrap CI(5-95%)。"
                 f"单状态<{_MIN_SHIP}次=样本不足仅参考。加速度用pen(稳定分母)·denom仅喂水平/趋势。"),
    }


def _fwd(pct: list, i: int, n: int):
    """信号 T=i·入场 T+1 收盘(e=i+1)·持有 n 日 → 出场 close_{e+n}。严格因果·需 n 天完整。"""
    e = i + 1
    if e + n >= len(pct):
        return None
    seg = pct[e + 1: e + n + 1]
    vals = [p for p in seg if p is not None]
    if len(vals) < n:
        return None
    prod = 1.0
    for p in vals:
        prod *= (1 + p / 100.0)
    return round((prod - 1) * 100, 3)


def _agg(vals: list, rng, n_boot: int) -> dict:
    """样本 → 均值/中位/胜率 + bootstrap 置信区间(5-95%)。"""
    if not vals:
        return {"n": 0, "mean": None, "median": None, "win": None, "win_ci": None, "mean_ci": None}
    a = np.array(vals, dtype=float)
    res = {"n": len(a), "mean": round(float(a.mean()), 2),
           "median": round(float(np.median(a)), 2), "win": round(float((a > 0).mean()) * 100, 1)}
    if len(a) >= 10:
        idx = rng.integers(0, len(a), size=(n_boot, len(a)))
        samp = a[idx]
        wins = (samp > 0).mean(axis=1) * 100
        means = samp.mean(axis=1)
        res["win_ci"] = [round(float(np.percentile(wins, 5)), 1), round(float(np.percentile(wins, 95)), 1)]
        res["mean_ci"] = [round(float(np.percentile(means, 5)), 2), round(float(np.percentile(means, 95)), 2)]
    else:
        res["win_ci"] = res["mean_ci"] = None
    return res


def _excess(vals: list, base: dict):
    if not vals or base.get("mean") is None:
        return None
    return round(float(np.mean(vals)) - base["mean"], 2)


def _excess_ci(state_vals: list, base_vals: list, rng, n_boot: int):
    """超额(状态 − 基准)的 bootstrap 95%CI：均值超额、胜率超额(pp)。样本不足→(None,None)。

    独立重采样状态样本与基准样本·各算统计量之差·取5-95分位。CI跨0=超额不显著。
    """
    if len(state_vals) < 10 or len(base_vals) < 30:
        return None, None
    s = np.array(state_vals, dtype=float)
    b = np.array(base_vals, dtype=float)
    dm, dw = [], []
    for _ in range(n_boot):
        ss = s[rng.integers(0, len(s), len(s))]
        bb = b[rng.integers(0, len(b), len(b))]
        dm.append(ss.mean() - bb.mean())
        dw.append((ss > 0).mean() * 100 - (bb > 0).mean() * 100)
    return ([round(float(np.percentile(dm, 5)), 2), round(float(np.percentile(dm, 95)), 2)],
            [round(float(np.percentile(dw, 5)), 1), round(float(np.percentile(dw, 95)), 1)])
