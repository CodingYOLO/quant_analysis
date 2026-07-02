"""申万 point-in-time 成分重建·纯函数测试（时点在册判定 / 已调出剔除 / 未来新增排除 / 兜底剔除）。

运行：.venv/bin/python tests/test_sw_membership.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.sw_membership import (JUNK_L2, clean_sectors,  # noqa: E402
                                        members_asof)


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


def _hist() -> pd.DataFrame:
    """合成成分历史：含 在册/已调出/未来调入/兜底行业 各种情况。"""
    rows = [
        # 半导体：A 老成分在册、B 2022调出、C 2025才调入(未来新增)、D 调出后...
        {"ts_code": "A", "l2_name": "半导体", "l1_name": "电子", "l3_name": "数字芯片",
         "in_date": "20200101", "out_date": None, "is_new": "Y"},
        {"ts_code": "B", "l2_name": "半导体", "l1_name": "电子", "l3_name": "封测",
         "in_date": "20190101", "out_date": "20220729", "is_new": "N"},
        {"ts_code": "C", "l2_name": "半导体", "l1_name": "电子", "l3_name": "数字芯片",
         "in_date": "20250601", "out_date": None, "is_new": "Y"},
        # 兜底行业：应被剔除
        {"ts_code": "E", "l2_name": "综合Ⅱ", "l1_name": "综合", "l3_name": "综合Ⅲ",
         "in_date": "20180101", "out_date": None, "is_new": "Y"},
    ]
    df = pd.DataFrame(rows)
    for c in ("in_date", "out_date"):
        df[c] = df[c].astype("string")
    return df


# ── 时点在册：2023年 半导体应含 A(在册)，不含 B(已2022调出)、不含 C(2025才进) ────────
def test_asof_point_in_time() -> None:
    m = members_asof(_hist(), "20230101", "L2")
    _assert("半导体" in m, "应有半导体")
    codes = set(m["半导体"])
    _assert(codes == {"A"}, f"2023在册应仅A(B已调出/C未来新增)，实得 {codes}")
    print("  ✓ 时点在册：排除已调出(B)+未来新增(C)")


# ── 未来新增股：2026年 C 已调入 → 半导体含 A,C ──────────────────────────────────
def test_future_addition_included_later() -> None:
    m = members_asof(_hist(), "20260101", "L2")
    _assert(set(m["半导体"]) == {"A", "C"}, f"2026应含A,C，实得 {m.get('半导体')}")
    # 而 2019 年 B 已在册、A/C 未进 → 仅 B
    m19 = members_asof(_hist(), "20190201", "L2")
    _assert(set(m19.get("半导体", [])) == {"B"}, f"2019应仅B，实得 {m19.get('半导体')}")
    print("  ✓ 未来新增股按 in_date 生效·历史调出股按 out_date 回填")


# ── 兜底行业剔除 ────────────────────────────────────────────────────────────────
def test_junk_excluded() -> None:
    m = members_asof(_hist(), "20230101", "L2", exclude_junk=True)
    _assert("综合Ⅱ" not in m, "兜底'综合Ⅱ'应剔除")
    m2 = members_asof(_hist(), "20230101", "L2", exclude_junk=False)
    _assert("综合Ⅱ" in m2, "关闭剔除时应保留")
    _assert("综合Ⅱ" in JUNK_L2, "综合Ⅱ 在兜底名单")
    _assert("综合Ⅱ" not in clean_sectors(_hist(), "L2"), "clean_sectors 应剔兜底")
    print("  ✓ 兜底行业(综合Ⅱ…)剔除")


# ── L1 上卷：半导体→电子(一级)·不剔兜底 ──────────────────────────────────────────
def test_level_rollup() -> None:
    m = members_asof(_hist(), "20260101", "L1")
    _assert("电子" in m and set(m["电子"]) == {"A", "C"}, f"L1电子应含A,C，实得 {m.get('电子')}")
    print("  ✓ L1 上卷聚合")


# ── 真实已调出个股时点正确性（600198式：19/07进·22/07出）──────────────────────────
# 验证"每个历史日重新取当天成分"：同一只票在其在册区间内的时点成分里出现、区间外消失。
def test_departed_stock_windowed() -> None:
    rows = [
        {"ts_code": "600198.SH", "l2_name": "通信设备", "l1_name": "通信", "l3_name": "通信设备Ⅲ",
         "in_date": "20190708", "out_date": "20220729", "is_new": "N"},          # 已调出
        {"ts_code": "X", "l2_name": "通信设备", "l1_name": "通信", "l3_name": "通信设备Ⅲ",
         "in_date": "20150101", "out_date": None, "is_new": "Y"},                 # 长期在册(锚)
    ]
    df = pd.DataFrame(rows)
    for c in ("in_date", "out_date"):
        df[c] = df[c].astype("string")

    def has600198(d: str) -> bool:
        return "600198.SH" in members_asof(df, d, "L2").get("通信设备", [])
    _assert(not has600198("20190101"), "进场前(2019-01)不应含600198")
    _assert(has600198("20200601"), "在册期(2020-06)应含600198")
    _assert(has600198("20211231"), "在册期(2021-末)应含600198")
    _assert(has600198("20220728"), "调出前一日(2022-07-28)应仍含")
    _assert(not has600198("20230101"), "调出后(2023-01)不应含600198")
    _assert(not has600198("20240101"), "调出后(2024-01)不应含600198")
    # 锚股全程在册
    _assert(all("X" in members_asof(df, d, "L2").get("通信设备", []) for d in
                ("20200601", "20230101")), "长期在册股应各时点都在")
    print("  ✓ 真实已调出股(600198式)：在册区间内纳入·区间外排除(逐日重取)")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n申万时点成分·重建测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
