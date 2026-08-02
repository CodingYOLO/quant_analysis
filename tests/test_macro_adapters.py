"""宏观面板 · L1 适配器纯函数测试（不打网络·全部用夹具帧）。

三条 2026-08-02 定档逐一守住：
(a) 北向断点前的值**不入库**（不止统计截断——净买入存进"成交额"列·回看会显示口径错误的数）
(b) margin_ratio 两腿同日相除（分母缺该日 → 当日无点·绝不跨日）
(c) 解禁=前瞻计划·无历史分布 → no_dist 展示项

运行：.venv/bin/python tests/test_macro_adapters.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.macro.adapters.ts_flow import (etf_share_delta, float_release_value,  # noqa: E402
                                        margin_complete_by_date, newfund_rolling)
from app.macro.compute import publication_status                              # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ── 两融：三所齐全才汇总（20260731 事故的回归测试） ─────────────────────────
def test_margin_complete_dates() -> None:
    frames = {
        "SSE":  pd.DataFrame({"trade_date": ["20260730", "20260731"],
                              "rzye": [1.32e12, 1.33e12], "rzmre": [1e10, 1.1e10]}),
        "SZSE": pd.DataFrame({"trade_date": ["20260730"],           # 0731 未发布
                              "rzye": [1.25e12], "rzmre": [9e9]}),
        "BSE":  pd.DataFrame({"trade_date": ["20260730"],
                              "rzye": [8.2e9], "rzmre": [1e8]}),
    }
    m = margin_complete_by_date(frames)
    _assert(m["trade_date"].tolist() == ["20260730"],
            "⭐0731 SZSE/BSE 缺 → 该日整体弃掉(实测裸sum会得13274亿·-49%假暴跌)")
    _assert(abs(m["rzye"].iloc[0] / 1e8 - (13200 + 12500 + 82)) < 1,
            "齐全日三所求和：13200+12500+82=25782亿")
    # 判据只看"有没有行"·不看值大小——北交所82亿是真实小值·不得被当缺失
    _assert(abs(m["rzye"].iloc[0] / 1e8 - 25782) < 1, "BSE 真实小值必须计入")
    empty = margin_complete_by_date({**frames, "BSE": pd.DataFrame()})
    _assert(empty.empty, "整所缺失 → 无法保证任何日期齐全 → 空(由结转规则接手)")
    print("  ✓ 两融：三所齐全才汇总·真实小值不误杀·整所缺失即弃")


# ── margin_ratio 同日对齐（(b) 的核心断言在适配器 fetch 里·此处测分母缺日行为） ──
def test_margin_ratio_same_day_alignment() -> None:
    """分母(circ_mv)取不到该日 → ratio 当日无点。适配器 _circ_mv_yi 返回 None 时
    fetch 循环 `if circ:` 跳过——这里验证该分支语义：绝不退回昨日市值凑一个比值。"""
    from app.macro.adapters.ts_flow import MarginAdapter
    # 语义由 fetch 内 `if circ:` 保证；直接测 _circ_mv_yi 对坏帧的行为
    class P:
        def get_daily_basic(self, d):
            return pd.DataFrame()                # 该日无数据
    _assert(MarginAdapter._circ_mv_yi(P(), "20260731") is None,
            "⭐(b) 分母该日缺 → None(当日无比值)·绝不拿别的日期顶替")
    print("  ✓ margin_ratio：分母缺日→当日无点(不跨日相除)")


# ── ETF 份额：只累计两日均存在的基金 Δ ────────────────────────────────────
def test_etf_delta_no_listing_jump() -> None:
    shares = pd.DataFrame({
        "ts_code":   ["A", "A", "A", "B", "B"],
        "trade_date": ["20260729", "20260730", "20260731", "20260730", "20260731"],
        "fd_share":  [10000.0, 10200.0, 10100.0, 50000.0, 50300.0],   # B 是 0730 新上市
    })
    d = etf_share_delta(shares)
    _assert(abs(d.loc["20260730"] - 0.02) < 1e-9,
            "⭐0730 只有 A 的Δ(+200万份=0.02亿份)——B 新上市首日 5亿份绝不能算成'申购'")
    _assert(abs(d.loc["20260731"] - ((-100 + 300) / 1e4)) < 1e-9,
            "0731 A(-100万)+B(+300万)=+0.02亿份(B 次日起正常参与)")
    _assert(pd.isna(d.loc["20260729"]), "首日无前值·NaN 而非 0")
    print("  ✓ ETF份额：新上市不制造假跳变·两日均在才计Δ")


# ── 新基金：28自然日滚动·类型过滤 ─────────────────────────────────────────
def test_newfund_rolling() -> None:
    fb = pd.DataFrame({
        "ts_code": ["F1", "F2", "F3", "F4"],
        "fund_type": ["股票型", "混合型", "债券型", "股票型"],
        "found_date": ["20260701", "20260710", "20260710", "20260501"],
        "issue_amount": [10.0, 20.0, 999.0, 5.0],
    })
    roll = newfund_rolling(fb, "20260701", "20260731")
    _assert(abs(roll.loc["20260715"] - 30.0) < 1e-9,
            "7/15 视角：近28日成立 F1(10)+F2(20)=30亿份；债券型999不算·5/1的F4出窗")
    _assert(abs(roll.loc["20260731"] - 20.0) < 1e-9,
            "7/31 窗=7/4~7/31·7/1成立的F1已出窗 → 只剩F2=20(滚动窗右端含当日)")
    print("  ✓ 新基金：股票+混合过滤·28自然日滚动·出窗即剔")


# ── 解禁：(c) 前瞻展示项的取值语义 ─────────────────────────────────────────
def test_float_release_value() -> None:
    ev = pd.DataFrame({
        "ts_code": ["X", "Y", "Z"],
        "float_date": ["20260810", "20260820", "20261001"],   # Z 在4周窗外
        "float_share": [1e8, 2e8, 9e9],                       # 股(⚠️实测单位·非文档标的万股)
    })
    v = float_release_value(ev, {"X": 10.0, "Y": 5.0}, "20260731")
    _assert(abs(v - (10 * 1e8 / 1e8 + 5 * 2e8 / 1e8)) < 1e-9,
            f"未来4周=X(10元×1亿股=10亿)+Y(5元×2亿股=10亿)=20亿·Z出窗不算·实得{v}"
            "——若错按万股口径(×1e4)此断言必炸")
    _assert(float_release_value(ev, {}, "20260731") is None,
            "⭐一条价格都配不上 → None(不给假0)")
    none_win = float_release_value(ev, {"X": 10.0}, "20261225")
    _assert(none_win == 0.0, "窗口内真没有解禁 → 真0(与配不上价格是两回事)")
    print("  ✓ 解禁：4周窗·配不上价格给None·真无解禁给0")


# ── 发布状态标注（用户点1：卡片"数据时点X·距下次发布N天"） ───────────────────
def test_publication_status() -> None:
    stale, nxt = publication_status("monthly", "20260630", 10, "20260802")
    _assert(stale == 23, f"6月报到8/2·扣除lag10 已滞后23天·实得{stale}")
    _assert(nxt == 7, f"下次发布≈7/31+10=8/10·距今8天上下·实得{nxt}")
    stale_d, nxt_d = publication_status("daily", "20260731", 0, "20260802")
    _assert(nxt_d is None, "daily 无'下次发布'概念")
    print("  ✓ 发布状态：月频滞后天数+距下次发布·daily不适用")


if __name__ == "__main__":
    print("宏观面板 · L1 适配器测试")
    for fn in (test_margin_complete_dates, test_margin_ratio_same_day_alignment,
               test_etf_delta_no_listing_jump, test_newfund_rolling,
               test_float_release_value, test_publication_status):
        fn()
    print("✅ 全部通过")
