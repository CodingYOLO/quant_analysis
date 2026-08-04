"""概念资金切换雷达纯函数测试（2026-08-03 概念资金页升级）。全部确定性构造。"""

from __future__ import annotations

from app.strategy.concept_flow import (
    _broad_reason,
    _group_by_overlap,
    _mad_scale,
    classify_switch_in,
    classify_switch_out,
)


# ── classify_switch_in ──────────────────────────────────────────────────────

def test_switch_in_basic_and_tags():
    # 基线安静(±1亿)·今日+20亿：异动+首次转正+创新高
    adj = [1.0, -1.0, 0.5, -0.5, 1.0, -1.0, 0.5, -0.5, 1.0, -1.0, 20.0]
    raw = [0.5, -1.5, 0.0, -1.0, 0.5, -1.5, 0.0, -1.0, -0.5, -1.5, 19.0]
    tags = classify_switch_in(adj, raw)
    assert "异动流入" in tags and "首次转正" in tags and any("新高" in t for t in tags)


def test_switch_in_quiet_day_no_trigger():
    adj = [1.0, -1.0, 0.5, -0.5, 1.0, -1.0, 0.5, -0.5, 1.0, -1.0, 1.5]
    raw = list(adj)
    assert classify_switch_in(adj, raw) == []


def test_switch_in_below_min_abs_no_trigger():
    # 相对显著但绝对量太小(<3亿)：小概念噪声不报
    adj = [0.1, -0.1, 0.05, -0.05, 0.1, -0.1, 0.05, -0.05, 0.1, -0.1, 2.0]
    raw = list(adj)
    assert classify_switch_in(adj, raw) == []


def test_switch_in_accel_tag():
    # 原始净额连三日递增(3→8→21)且今日相对异动 → 连2日加速
    adj = [0.5, -0.5, 0.2, -0.2, 0.5, -0.5, 0.2, -0.2, 1.0, 6.0, 20.0]
    raw = [0.5, -0.5, 0.2, -0.2, 0.5, -0.5, 0.2, -0.2, 3.0, 8.0, 21.0]
    tags = classify_switch_in(adj, raw)
    assert "连2日加速" in tags


def test_switch_in_short_history_no_trigger():
    assert classify_switch_in([1.0, 2.0, 50.0], [1.0, 2.0, 50.0]) == []


# ── classify_switch_out ─────────────────────────────────────────────────────

def test_switch_out_first_day():
    adj = [5.0, 6.0, 4.0, 5.0, 6.0, 4.0, 5.0, 6.0, 5.0, 4.0, -12.0]
    raw = [8.0, 9.0, 7.0, 8.0, 9.0, 7.0, 8.0, 9.0, 8.0, 7.0, -9.0]
    assert classify_switch_out(adj, raw, cum10=80.0) == ["首日转出"]


def test_switch_out_streak():
    adj = [5.0, 6.0, 4.0, 5.0, 6.0, 4.0, 5.0, 6.0, -8.0, -9.0, -12.0]
    raw = [8.0, 9.0, 7.0, 8.0, 9.0, 7.0, 8.0, 9.0, -5.0, -6.0, -9.0]
    tags = classify_switch_out(adj, raw, cum10=50.0)
    assert "连3日流出" in tags


def test_switch_out_big_outflow_fallback():
    # 高波动主线：MAD被撑大·路径A不触发，但单日流出=10日均额9倍 → 路径B兜底
    adj = [40.0, -35.0, 30.0, -25.0, 45.0, -40.0, 35.0, -30.0, 50.0, -20.0, -60.0]
    raw = [60.0, -15.0, 50.0, -5.0, 65.0, -20.0, 55.0, -10.0, 70.0, 0.0, -180.0]
    tags = classify_switch_out(adj, raw, cum10=207.0)
    assert "大额流出" in tags


def test_switch_out_small_pool_no_fallback():
    # cum10≤20 不启用大额兜底（小主线的"1.5倍日均"没有量级意义）
    adj = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, -2.5]
    raw = list(adj)
    assert "大额流出" not in classify_switch_out(adj, raw, cum10=10.0)


# ── 同族聚合 / 宽概念 ────────────────────────────────────────────────────────

def test_group_by_overlap_merges_and_truncates():
    rows = [{"concept": "机器人", "today": 50.0},
            {"concept": "人形机器人", "today": 30.0},
            {"concept": "白酒", "today": 20.0}]
    mmap = {"机器人": [f"c{i}" for i in range(100)],
            "人形机器人": [f"c{i}" for i in range(40)],       # 40/40 全含于机器人
            "白酒": ["x1", "x2", "x3", "x4", "x5"]}
    out = _group_by_overlap(rows, mmap)
    assert [r["concept"] for r in out] == ["机器人", "白酒"]
    assert out[0]["kin"] == ["人形机器人"] and out[0]["kin_n"] == 1


def test_group_by_overlap_no_map_passthrough():
    rows = [{"concept": "A", "today": 1.0}, {"concept": "B", "today": 0.5}]
    assert _group_by_overlap(rows, {}) == rows


def test_broad_reason_ths_house_index():
    assert _broad_reason("同花顺中特估100") == "宽基指数"
    assert _broad_reason("同花顺漂亮100") == "宽基指数"
    assert _broad_reason("减速器") is None


def test_mad_scale_floor():
    assert _mad_scale([0.0, 0.0, 0.0, 0.0, 0.0]) == 0.8      # 退化序列吃下限·防全员误报


# ── classify_flow_shape：3日路径形态词典（2026-08-04 主题战场）──────────────────

from app.strategy.concept_flow import classify_flow_shape, THEME_FAMILIES


def _hist(*tail):
    # 安静基线(±1亿·MAD吃下限0.8) + 指定尾部
    return [1.0, -1.0, 0.5, -0.5, 1.0, -1.0, 0.5] + list(tail)


def test_shape_accel_in():
    assert classify_flow_shape(_hist(2.0, 5.0, 12.0)) == "🔥加速流入"


def test_shape_accel_bleed():
    assert classify_flow_shape(_hist(-2.0, -5.0, -12.0)) == "💨加速失血"


def test_shape_spike_fade():
    # 博主图的 +19→+2：昨日≥2×MAD·今日缩到四成以下
    assert classify_flow_shape(_hist(2.0, 19.0, 2.0)) == "↘冲高回落"


def test_shape_turn_negative():
    assert classify_flow_shape(_hist(1.0, 2.0, -3.0)) == "🔴转负"


def test_shape_reflow_after_bleed():
    # 前5日净流出为主·今日转正 = 资金回流(区别于普通转正)
    nets = [1.0, -1.0, -3.0, -4.0, -2.0, -3.0, -2.0, -1.5, -0.5, 2.0]
    assert classify_flow_shape(nets) == "🔄资金回流"


def test_shape_streak_positive():
    # 基线尾部0.5也是正·连正计数=4(词典如实数·不截断到3)
    assert classify_flow_shape(_hist(2.0, 3.0, 2.5)) == "🟢连4日正"


def test_shape_stall():
    assert classify_flow_shape(_hist(0.1, -0.1, 0.1)) == "→停滞"


def test_shape_insufficient():
    assert classify_flow_shape([1.0, None, 2.0]) == "·"


def test_theme_families_no_dup_members():
    seen = {}
    for f in THEME_FAMILIES:
        assert f["bucket"] in ("tech", "defense", "neutral")
        for m in f["members"]:
            assert m not in seen, f"{m} 同时在 {seen.get(m)} 与 {f['family']}"
            seen[m] = f["family"]


# ── stock_gate：个股暗流池硬门槛（2026-08-04 暗流页重设计）────────────────────

from app.strategy.ambush_board import stock_gate


def test_stock_gate_pass():
    assert stock_gate({"main_flow_3d": 1.2, "change_7d": 1.0, "dist_high": -12.0})


def test_stock_gate_reject_no_flow():
    assert not stock_gate({"main_flow_3d": 0.2, "change_7d": 1.0, "dist_high": -12.0})


def test_stock_gate_reject_already_moved():
    assert not stock_gate({"main_flow_3d": 2.0, "change_7d": 5.0, "dist_high": -12.0})


def test_stock_gate_reject_near_high():
    # 距60日高点仅3%=冲高位·不是低吸
    assert not stock_gate({"main_flow_3d": 2.0, "change_7d": 1.0, "dist_high": -3.0})


def test_stock_gate_none_fields():
    assert not stock_gate({"main_flow_3d": None, "change_7d": None, "dist_high": None})
