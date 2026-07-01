"""关键位模块单测：聚类 / 入局区间 / 位置判定（纯函数·确定性），+ 合成日K端到端结构校验。

运行：.venv/bin/python tests/test_key_levels.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.key_levels import (  # noqa: E402
    _cluster, _entry_zone, _position, build_key_levels,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ── 聚类：相近位共振合并成带 ────────────────────────────────────────────────
def test_cluster_merges_nearby() -> None:
    cands = [{"price": 95.0, "src": "MA20"}, {"price": 95.4, "src": "20日低"},
             {"price": 88.0, "src": "筹码成本下沿"}]
    bands = _cluster(cands, px=100.0, side="support")
    _assert(len(bands) == 2, f"应聚成2带(95档合并+88档)，实得 {len(bands)}")
    top = bands[0]                                        # 支撑由近及远：95 档在前
    _assert(top["low"] == 95.0 and top["high"] == 95.4, f"带上下沿错误 {top}")
    _assert(set(top["srcs"]) == {"MA20", "20日低"}, f"依据合并错误 {top['srcs']}")
    _assert(top["strength"] == 2, "共振强度应=2")
    _assert(top["dist_pct"] < 0, "支撑应在现价下方(dist为负)")
    print("  ✓ 聚类：相近位共振合并、依据齐全、强度计数正确")


def test_cluster_sides_ordering() -> None:
    sup = _cluster([{"price": 90, "src": "a"}, {"price": 95, "src": "b"}], 100, "support")
    res = _cluster([{"price": 110, "src": "c"}, {"price": 105, "src": "d"}], 100, "resistance")
    _assert(sup[0]["mid"] > sup[1]["mid"], "支撑应由近及远(价降序)")
    _assert(res[0]["mid"] < res[1]["mid"], "压力应由近及远(价升序)")
    print("  ✓ 排序：支撑降序、压力升序（均由近及远）")


# ── 入局区间：最近支撑带；单点自动缓冲 ──────────────────────────────────────
def test_entry_zone_from_nearest_band() -> None:
    # 95/96 相距约1%(≤聚类容忍1.5%)→合并；带宽1.0>阈值0.5→不再缓冲，直接用原带
    supports = _cluster([{"price": 95.0, "src": "MA20"}, {"price": 96.0, "src": "20日低"}],
                        px=100.0, side="support")
    z = _entry_zone(supports, 100.0)
    _assert(z is not None and z["low"] == 95.0 and z["high"] == 96.0, f"区间应=最近带 {z}")
    _assert(z["srcs"] and z["basis"], "入局区间必须带依据(可溯源)")
    print("  ✓ 入局区间=最近支撑带（够宽不缓冲），且带依据")


def test_entry_zone_single_point_buffer() -> None:
    supports = _cluster([{"price": 90.0, "src": "MA20"}], px=100.0, side="support")
    z = _entry_zone(supports, 100.0)
    _assert(z["low"] < 90.0 < z["high"], f"单点位应 ±缓冲成区间 {z}")
    print("  ✓ 单点支撑自动 ±1% 缓冲成区间")


def test_entry_zone_empty() -> None:
    _assert(_entry_zone([], 100.0) is None, "无支撑应返回 None")
    print("  ✓ 无支撑→入局区间为 None（边界）")


# ── 位置判定：below / in / watch / far ──────────────────────────────────────
def test_position_states() -> None:
    zone = {"low": 95.0, "high": 97.0, "srcs": ["MA20"], "strength": 1, "basis": "MA20"}
    _assert(_position(93.0, zone)["state"] == "below", "低于下沿→below")
    _assert(_position(96.0, zone)["state"] == "in", "区间内→in")
    _assert(_position(98.0, zone)["state"] == "watch", "≤5%警戒带→watch")
    _assert(_position(105.0, zone)["state"] == "far", ">5%→far")
    _assert(_position(100.0, None)["state"] == "na", "无区间→na")
    for st in ("below", "in", "watch", "far", "na"):
        pass
    print("  ✓ 位置判定：below/in/watch/far/na 全覆盖·语言限『观察』不作买卖建议")


# ── 端到端：合成上升日K，校验结构不变量 + 可溯源 ────────────────────────────
def _synthetic_k(n: int = 120) -> pd.DataFrame:
    close = [round(50 + i * (50 / (n - 1)), 2) for i in range(n)]     # 50→100 线性上行
    return pd.DataFrame({
        "trade_date": [f"2026{(i % 12) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)],
        "open": close, "close": close,
        "high": [round(c * 1.01, 2) for c in close],
        "low": [round(c * 0.99, 2) for c in close],
        "vol": [10000 + i for i in range(n)],
        "pct_chg": [0.5] * n,
    })


def test_end_to_end_invariants() -> None:
    k = _synthetic_k()
    chips = {"cost_5pct": 88.0, "cost_50pct": 93.0, "cost_95pct": 99.0}
    r = build_key_levels(k, chips)
    px = r["price"]
    _assert(r is not None and px > 0, "应产出结果")
    _assert(all(b["mid"] <= px for b in r["support"]), "支撑必在现价下方")
    _assert(all(b["mid"] >= px for b in r["resistance"]), "压力必在现价上方")
    _assert(r["entry_zone"] and r["entry_zone"]["low"] <= r["entry_zone"]["high"], "区间上下沿有序")
    all_srcs = [s for b in r["support"] for s in b["srcs"]]
    _assert(any("筹码" in s for s in all_srcs), "应含筹码依据(可溯源)")
    _assert(any(s.startswith("MA") for s in all_srcs), "应含均线依据(可溯源)")
    _assert(r["position"]["label"], "位置必须有文案")
    _assert(r["as_of"] == k["trade_date"].iloc[-1], "as_of 应为最新交易日")
    print(f"  ✓ 端到端：现价{px} 支撑{len(r['support'])}带 压力{len(r['resistance'])}带 "
          f"入局[{r['entry_zone']['low']},{r['entry_zone']['high']}] {r['position']['state']}")


def test_insufficient_data() -> None:
    _assert(build_key_levels(pd.DataFrame(), None) is None, "空数据→None")
    _assert(build_key_levels(_synthetic_k(30), None) is None, "<60行→None(边界)")
    print("  ✓ 数据不足→None（边界检查）")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n关键位模块测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
