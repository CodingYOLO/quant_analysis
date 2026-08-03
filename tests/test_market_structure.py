"""market_structure 纯函数测试：交叉/吞没/连阳阴/切换雷达（2026-08-03 板块诊断升级）。

全部确定性构造（回测教训：随机构造会撞上分位巧合·断言要可推导）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategy.market_structure import engulfing, fresh_cross, streak
from app.strategy.sector_mtf import _rotation


# ── fresh_cross：只认"最新一根新发生" ────────────────────────────────────────

def test_fresh_cross_gold():
    fast = pd.Series([1.0, 2.0, 4.0])
    slow = pd.Series([3.0, 3.0, 3.0])
    assert fresh_cross(fast, slow) == "gold"


def test_fresh_cross_dead():
    fast = pd.Series([5.0, 4.0, 2.0])
    slow = pd.Series([3.0, 3.0, 3.0])
    assert fresh_cross(fast, slow) == "dead"


def test_fresh_cross_no_event_when_already_above():
    # 早已在上方=存量状态·不是新事件（这正是"增量视角"的核心口径）
    fast = pd.Series([4.0, 5.0, 6.0])
    slow = pd.Series([3.0, 3.0, 3.0])
    assert fresh_cross(fast, slow) == ""


def test_fresh_cross_touch_then_break_counts():
    # 前一根恰好相等(<=)也算"另一侧"·当根突破算金叉
    fast = pd.Series([2.0, 3.0, 4.0])
    slow = pd.Series([3.0, 3.0, 3.0])
    assert fresh_cross(fast, slow) == "gold"


def test_fresh_cross_nan_and_short():
    assert fresh_cross(pd.Series([1.0]), pd.Series([2.0])) == ""
    fast = pd.Series([np.nan, 4.0])
    slow = pd.Series([3.0, 3.0])
    assert fresh_cross(fast, slow) == ""


# ── engulfing：实体覆盖口径 ─────────────────────────────────────────────────

def test_engulfing_yang_bao_yin():
    # 前阴(10→9)·当阳(8.8→10.2)·实体完全覆盖
    assert engulfing(10.0, 9.0, 8.8, 10.2) == "阳包阴"


def test_engulfing_yin_bao_yang():
    # 前阳(9→10)·当阴(10.2→8.8)
    assert engulfing(9.0, 10.0, 10.2, 8.8) == "阴包阳"


def test_engulfing_partial_cover_is_not():
    # 当阳收盘10.2>前开10 但开盘9.5>前收9=没包住下沿
    assert engulfing(10.0, 9.0, 9.5, 10.2) == ""


def test_engulfing_same_direction_is_not():
    # 两根同为阳线不构成吞没
    assert engulfing(9.0, 10.0, 10.0, 11.0) == ""


def test_engulfing_nan():
    assert engulfing(float("nan"), 9.0, 8.8, 10.2) == ""


# ── streak：连阳连阴（≥3才报）──────────────────────────────────────────────

def _kline(pairs):
    """pairs: [(open, close), ...] 时间升序 → (closes, opens)。"""
    o = pd.Series([p[0] for p in pairs], dtype=float)
    c = pd.Series([p[1] for p in pairs], dtype=float)
    return c, o


def test_streak_3_yang():
    c, o = _kline([(10, 9), (9, 10), (10, 11), (11, 12)])   # 尾部连3阳
    assert streak(c, o) == "连3阳"


def test_streak_4_yin():
    c, o = _kline([(10, 11), (11, 10), (10, 9), (9, 8), (8, 7)])
    assert streak(c, o) == "连4阴"


def test_streak_below_threshold():
    c, o = _kline([(10, 9), (9, 10), (10, 11)])             # 只有连2阳
    assert streak(c, o) == ""


def test_streak_capped_at_nmax():
    c, o = _kline([(i, i + 1) for i in range(10)])           # 连10阳→报上限6
    assert streak(c, o) == "连6阳"


def test_streak_doji_breaks():
    c, o = _kline([(9, 10), (10, 10), (10, 11), (11, 12)])   # 十字星截断·尾部只剩2阳
    assert streak(c, o) == ""


# ── _rotation：排名跃迁数学 ─────────────────────────────────────────────────

def _mk_rows(n=40):
    """构造 n 个板块：ret20 按 i 递减(排名=i)·ret5 默认同排名(jump=0)。"""
    return [{"sector": f"S{i}", "kind": "industry", "monthly_dir": "月线向上",
             "top_count": 0, "m_pattern": "", "w_event": "",
             "ret5": float(n - i), "ret20": float(n - i)} for i in range(n)]


def test_rotation_no_jump():
    rot = _rotation(_mk_rows())
    assert rot == {"in": [], "out": []}


def test_rotation_in_and_out():
    rows = _mk_rows(40)
    # S30：ret20 排名30 → ret5 拉到全场第一(排名0)=跃升30位
    rows[30]["ret5"] = 999.0
    # S2：ret20 排名2 → ret5 砸到全场最后=下滑约37位
    rows[2]["ret5"] = -999.0
    rot = _rotation(rows, jump_th=15)
    assert [r["sector"] for r in rot["in"]] == ["S30"]
    assert rot["in"][0]["jump"] == 30
    assert [r["sector"] for r in rot["out"]] == ["S2"]
    assert "排名跃升30位" in rot["in"][0]["evidence"]


def test_rotation_evidence_carries_structure():
    rows = _mk_rows(40)
    rows[30]["ret5"] = 999.0
    rows[30]["m_pattern"] = "阳包阴(本月未收官·收官前可变)"
    rows[30]["top_count"] = 2
    rot = _rotation(rows, jump_th=15)
    ev = rot["in"][0]["evidence"]
    assert "阳包阴" in ev and "未收官" in ev and "见顶2/3" in ev


def test_rotation_small_sample_returns_empty():
    assert _rotation(_mk_rows(10)) == {"in": [], "out": []}


# ── structure_row：上月收官形态的可见性（新月前3日报事件·之后转状态列）────────────

def _daily_k(months):
    """months: [(yyyymm, [(open,close),...]), ...] → 极简日线DataFrame(每月n根)。"""
    rows = []
    for ym, bars in months:
        for i, (o, c) in enumerate(bars, 1):
            hi, lo = max(o, c) + 0.5, min(o, c) - 0.5
            rows.append({"trade_date": f"{ym}{i:02d}", "open": o, "high": hi,
                         "low": lo, "close": c, "vol": 100.0})
    return pd.DataFrame(rows)


def test_pattern_closed_reported_early_new_month():
    from app.strategy.market_structure import structure_row
    # 12个平月热身 + 前月大阳(9→11) + 上月阴包阳(11.5→8.5) + 本月才1根小K
    warm = [(f"2025{m:02d}", [(10.0, 10.0)] * 4) for m in range(1, 13)]
    k = _daily_k(warm + [("202606", [(9.0, 11.0)] * 4),
                         ("202607", [(11.5, 8.5)] * 4),
                         ("202608", [(8.6, 8.7)])])
    st = structure_row(k, "T", unfinished_month=True)
    assert st["monthly"].get("pattern_closed", "").startswith("阴包阳")
    assert any("上月收官·月线阴包阳" in e["event"] for e in st["events"])


def test_pattern_closed_no_event_mid_month():
    from app.strategy.market_structure import structure_row
    warm = [(f"2025{m:02d}", [(10.0, 10.0)] * 4) for m in range(1, 13)]
    k = _daily_k(warm + [("202606", [(9.0, 11.0)] * 4),
                         ("202607", [(11.5, 8.5)] * 4),
                         ("202608", [(8.6, 8.7)] * 5)])   # 本月已5根>3
    st = structure_row(k, "T", unfinished_month=True)
    assert st["monthly"].get("pattern_closed", "").startswith("阴包阳")   # 状态列常驻
    assert not any("上月收官" in e["event"] for e in st["events"])        # 事件不再重复播报
