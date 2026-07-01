"""
个股「关键位」：从前复权日K + 筹码分布，自算**可溯源**的支撑带 / 压力带 / 入局区间。

设计目标（对标稳智AI「入局区间」，但补其硬伤）：
  - 稳智的入局区间来源不明、且明显不随行情更新（长电入局75-78/现价106、寒武纪280-310/现价1494）。
  - 本模块的每一档位都由**当日数据**算出（均线 / 前低 / 筹码密集区），天然贴近现价、不会过时，
    且每个数字都带「依据」标签 → 可验证、可溯源（铁律 [[ai-data-must-be-sourced]]）。

纯函数、无副作用、无外部 IO（数据由上层 stock_profile 传入），便于单元测试。
只标「位置」，**不预测涨跌、不构成买卖建议**（铁律）。
"""

from __future__ import annotations

import pandas as pd

# 多个方法在相近价位共振 → 合并成一条「带」的容忍度（A股个股波动，1.5% 较稳）
_CLUSTER_TOL = 0.015
# 略高于入局区间上沿、但仍值得等回踩的「警戒带」宽度（借鉴稳智 5% 警戒带）
_WATCH_BAND = 0.05
# 每侧最多展示的带数量（由近及远）
_MAX_BANDS = 3


# ── 对外主函数 ──────────────────────────────────────────────────────────────
def build_key_levels(k: pd.DataFrame, chips: dict | None = None,
                     price: float | None = None) -> dict | None:
    """
    计算关键位。k 为前复权日K（需含 close/high/low，≥60 行）；chips 为筹码快照（可空）。
    price 缺省用最新收盘价（P1 盘中可传实时价覆盖）。
    返回 {price, as_of, support[], resistance[], entry_zone, position} 或 None（数据不足）。
    """
    if k is None or len(k) < 60:
        return None
    close = k["close"].astype(float)
    px = float(price) if price and price > 0 else float(close.iloc[-1])
    if px <= 0:
        return None

    cands = _candidates(k, chips)                       # 全部候选位（含依据·含方位约束 side）
    sup_c = [c for c in cands if c["side"] == "sup" or (c["side"] == "any" and c["price"] <= px * 1.002)]
    res_c = [c for c in cands if c["side"] == "res" or (c["side"] == "any" and c["price"] >= px * 0.998)]
    supports = _cluster(sup_c, px, side="support")
    resists = _cluster(res_c, px, side="resistance")
    zone = _entry_zone(supports, px)
    return {
        "price": round(px, 2),
        "as_of": str(k["trade_date"].iloc[-1]),
        "support": supports[:_MAX_BANDS],
        "resistance": resists[:_MAX_BANDS],
        "entry_zone": zone,
        "position": _position(px, zone),
    }


# ── 候选位收集（均线 / 前低前高 / 筹码密集区）──────────────────────────────
def _candidates(k: pd.DataFrame, chips: dict | None) -> list[dict]:
    """收集所有可溯源候选位。每项 {price, src, side}；side=sup/res/any 约束方位。"""
    out: list[dict] = []
    out += _ma_levels(k)
    out += _swing_levels(k)
    out += _chip_levels(chips)
    return [c for c in out if c["price"] and c["price"] > 0]


def _ma_levels(k: pd.DataFrame) -> list[dict]:
    """均线：MA10/MA20/MA60。现价在其上=支撑、其下=压力 → side=any 交由按价分侧。"""
    close = k["close"].astype(float)
    out = []
    for n in (10, 20, 60):
        if len(close) >= n:
            out.append({"price": round(float(close.tail(n).mean()), 2), "src": f"MA{n}", "side": "any"})
    return out


def _swing_levels(k: pd.DataFrame) -> list[dict]:
    """前低/前高：20/60日 高低点。前低恒≤现价→只作支撑；前高恒≥现价→只作压力(方位固定)。"""
    high, low = k["high"].astype(float), k["low"].astype(float)
    out = []
    for n, tag in ((20, "20日"), (60, "60日")):
        if len(k) >= n:
            out.append({"price": round(float(low.tail(n).min()), 2), "src": f"{tag}低", "side": "sup"})
            out.append({"price": round(float(high.tail(n).max()), 2), "src": f"{tag}高", "side": "res"})
    return out


def _chip_levels(chips: dict | None) -> list[dict]:
    """筹码密集区：成本下沿/主力平均成本/套牢峰 → side=any 按价分侧。"""
    if not chips:
        return []
    m = {"cost_5pct": "筹码下沿", "cost_50pct": "主力平均成本", "cost_95pct": "筹码上沿"}
    return [{"price": chips[k], "src": v, "side": "any"} for k, v in m.items() if chips.get(k)]


# ── 聚类：相近价位合并成「带」，共振越多越强 ────────────────────────────────
def _cluster(cands: list[dict], px: float, side: str) -> list[dict]:
    """把相近候选位合并成带；support 由近及远(价降序)，resistance 由近及远(价升序)。"""
    if not cands:
        return []
    reverse = side == "support"                          # 支撑：离现价近=价高→降序
    items = sorted(cands, key=lambda c: c["price"], reverse=reverse)
    bands: list[dict] = []
    for c in items:
        cur = bands[-1] if bands else None
        if cur and abs(c["price"] - cur["_ref"]) / cur["_ref"] <= _CLUSTER_TOL:
            _merge_into(cur, c)
        else:
            bands.append({"_ref": c["price"], "low": c["price"], "high": c["price"],
                          "srcs": [c["src"]]})
    for b in bands:
        _finalize_band(b, px)
    return bands


def _merge_into(band: dict, c: dict) -> None:
    """把候选 c 并入已有带：更新上下沿与依据列表。"""
    band["low"] = min(band["low"], c["price"])
    band["high"] = max(band["high"], c["price"])
    if c["src"] not in band["srcs"]:
        band["srcs"].append(c["src"])


def _finalize_band(band: dict, px: float) -> None:
    """定稿一条带：算中值、离现价距离%、强度(共振依据数)，去掉内部临时字段。"""
    band.pop("_ref", None)
    band["mid"] = round((band["low"] + band["high"]) / 2, 2)
    band["dist_pct"] = round((band["mid"] / px - 1) * 100, 2)   # 负=下方，正=上方
    band["strength"] = len(band["srcs"])                        # 共振方法数=强度


# ── 入局区间：以「最近支撑带」为基准 ────────────────────────────────────────
def _entry_zone(supports: list[dict], px: float) -> dict | None:
    """入局区间 = 最近的支撑带 [low, high]；单点带自动 ±1% 缓冲。全部可溯源。"""
    if not supports:
        return None
    b = supports[0]
    low, high = b["low"], b["high"]
    if high - low < px * 0.005:                          # 单点/过窄→给 ±1% 缓冲成区间
        low, high = low * 0.99, high * 1.01
    return {
        "low": round(low, 2), "high": round(high, 2),
        "srcs": b["srcs"], "strength": b["strength"],
        "basis": "、".join(f"{s}" for s in b["srcs"]),
    }


# ── 现价相对入局区间的位置判定（借鉴稳智格式，但语言限「观察」不作买卖建议）──
def _position(px: float, zone: dict | None) -> dict:
    """判定现价状态：below / in / watch(5%警戒带) / far。只描述位置。"""
    if not zone:
        return {"state": "na", "label": "数据不足·无法定位入局区间", "dist_pct": None}
    low, high = zone["low"], zone["high"]
    if px < low:
        return {"state": "below", "dist_pct": round((px / low - 1) * 100, 2),
                "label": f"低于入局区间下沿 {(px / low - 1) * 100:+.1f}%（跌破支撑带·破位需重估，非买点）"}
    if px <= high:
        return {"state": "in", "dist_pct": 0.0,
                "label": "现价落在入局区间内（回调至支撑带·可低吸观察）"}
    over = (px / high - 1) * 100
    if px <= high * (1 + _WATCH_BAND):
        return {"state": "watch", "dist_pct": round(over, 2),
                "label": f"略高于入局区间 +{over:.1f}%（5%警戒带内·随时回踩，可蹲守）"}
    return {"state": "far", "dist_pct": round(over, 2),
            "label": f"已远离入局区间 +{over:.1f}%（未回调·不追高·等回踩）"}
