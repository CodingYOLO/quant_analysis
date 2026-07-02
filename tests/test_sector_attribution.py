"""大类资金归因·资金地图 纯函数测试（市值分档边界 / 大类映射 / 两条分组正交不重复）。

运行：.venv/bin/python tests/test_sector_attribution.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.sector_attribution import (_CAP_TIERS, _L1_TO_MACRO,  # noqa: E402
                                             CAP_NAMES, MACROS, _cap_tier)


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


# ── 市值分档边界（500/100/30 亿）─────────────────────────────────────────────────
def test_cap_tier_bounds() -> None:
    _assert(_cap_tier(800) == "大盘≥500亿", "800亿→大盘")
    _assert(_cap_tier(500) == "大盘≥500亿", "边界500→大盘")
    _assert(_cap_tier(300) == "中盘100-500亿", "300→中盘")
    _assert(_cap_tier(100) == "中盘100-500亿", "边界100→中盘")
    _assert(_cap_tier(50) == "小盘30-100亿", "50→小盘")
    _assert(_cap_tier(10) == "微盘<30亿", "10→微盘")
    _assert(_cap_tier(0) == "微盘<30亿", "0→微盘")
    print("  ✓ 市值分档：500/100/30亿边界正确")


# ── 每只股恰好归一个大类 + 一个市值档（正交·不重复计数·不漏算）──────────────────────
def test_orthogonal_single_assignment() -> None:
    # 小市值电子股：大类=科技(不因小市值被抠出)·市值档=微盘 —— 两条各归一次
    macro = _L1_TO_MACRO.get("电子", "其他")
    cap = _cap_tier(15)
    _assert(macro == "科技" and cap == "微盘<30亿", f"小盘电子应(科技,微盘)，实得({macro},{cap})")
    # 未映射一级 → 其他(不丢)
    _assert(_L1_TO_MACRO.get("综合", "其他") == "其他", "综合→其他(兜底)")
    # 大类与市值档名称集合不相交(确属两条正交维度)
    _assert(not (set(MACROS) & set(CAP_NAMES)), "大类与市值档不应有同名(两条独立维度)")
    print("  ✓ 正交单归属：小盘电子=(科技,微盘)·各维度恰归一次·未映射→其他")


# ── 覆盖完整性：主要一级都已映射到 8 大类之一 ───────────────────────────────────────
def test_macro_coverage() -> None:
    _assert(set(_L1_TO_MACRO.values()) <= set(MACROS), "所有映射值须在 MACROS 内")
    key_l1 = ["电子", "医药生物", "食品饮料", "银行", "有色金属", "电力设备", "公用事业"]
    for l1 in key_l1:
        _assert(l1 in _L1_TO_MACRO, f"主要一级 {l1} 应有大类映射")
    _assert(len(_CAP_TIERS) == 4, "市值分 4 档")
    print("  ✓ 大类映射覆盖主要申万一级·市值4档")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n大类资金归因·资金地图测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
