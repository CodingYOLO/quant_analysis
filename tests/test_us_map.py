"""美股→A股映射 纯函数测试（2026-08-05）。重点守时间轴对齐这条命门。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategy.us_map import (ALL_SYMS, CHAINS, divergence, link_stats,
                                 next_day_align)


def test_next_day_align_basic():
    # 美股T日 → A股T之后第一个交易日
    us = pd.Series([1.0, 2.0, 3.0], index=["20260801", "20260804", "20260805"])
    cn = pd.Series([10.0, 20.0, 30.0], index=["20260801", "20260804", "20260805"])
    a = next_day_align(us, cn)
    assert a["20260801"] == 20.0        # 美股8/1 → A股8/4
    assert a["20260804"] == 30.0        # 美股8/4 → A股8/5
    assert np.isnan(a["20260805"])      # 美股8/5 之后A股还没数据


def test_next_day_align_holiday_gap():
    # A股休市(8/4缺)：美股8/1 与 8/4 都跳到A股8/5——长假期间多个美股日映射同一A股日，
    # 属预期行为(一年仅数次·对250日相关系数影响可忽略)，不做去重以免丢样本。
    us = pd.Series([1.0, 2.0], index=["20260801", "20260804"])
    cn = pd.Series([10.0, 30.0], index=["20260801", "20260805"])
    a = next_day_align(us, cn)
    assert a["20260801"] == 30.0 and a["20260804"] == 30.0


def test_next_day_align_tail_is_nan():
    # 最后一个美股日之后还没有A股数据 → NaN(不脑补·由link_stats的dropna剔除)
    us = pd.Series([1.0, 2.0], index=["20260804", "20260805"])
    cn = pd.Series([10.0, 20.0], index=["20260804", "20260805"])
    assert np.isnan(next_day_align(us, cn)["20260805"])


def test_next_day_align_never_same_day():
    # 铁律：绝不同日对齐（同日=用未来数据·系数虚高）
    us = pd.Series([5.0], index=["20260804"])
    cn = pd.Series([99.0], index=["20260804"])
    assert np.isnan(next_day_align(us, cn)["20260804"])


def test_link_stats_perfect_positive():
    idx = [f"2026{i:04d}" for i in range(1, 61)]
    x = pd.Series(np.linspace(-3, 3, 60), index=idx)
    st = link_stats(x, x * 2)                        # 完全正相关
    assert st["corr"] > 0.99 and st["n"] == 60


def test_link_stats_small_sample_returns_none():
    x = pd.Series([1.0, 2.0, 3.0], index=["a", "b", "c"])
    st = link_stats(x, x)
    assert st["corr"] is None and st["n"] == 3       # <30 不给系数


def test_link_stats_hit_rate_needs_20():
    idx = [f"2026{i:04d}" for i in range(1, 61)]
    us = pd.Series([3.0] * 10 + [0.1] * 50, index=idx)   # 只有10个大涨日
    cn = pd.Series([1.0] * 60, index=idx)
    st = link_stats(us, cn)
    assert st["hit_up"] is None and st["n_up"] == 10     # 样本<20 不给命中率


def test_divergence_labels():
    assert divergence(3.0, -0.5)["code"] == "catchup"    # 美股大涨A股没跟
    assert divergence(-3.0, 0.5)["code"] == "resist"     # A股抗跌
    assert divergence(3.0, 1.0)["code"] == "sync"
    assert divergence(-3.0, -1.0)["code"] == "drag"
    assert divergence(0.5, 0.2)["code"] == ""            # 小波动不标
    assert divergence(None, 1.0)["code"] == ""


def test_mapping_table_sane():
    assert len(ALL_SYMS) == len(set(ALL_SYMS)), "美股标的重复"
    for ch in CHAINS:
        assert ch["chain"] and ch["logic"]
        for it in ch["items"]:
            assert it["sym"].isupper() and it["cn"] and it["note"]
            assert it["concepts"], f"{it['sym']} 无对应概念"
    # 弱映射必须自带警示（诚实红线：KO→白酒不能装成产业链）
    weak = [it for ch in CHAINS for it in ch["items"] if it["sym"] == "KO"][0]
    assert "⚠️" in weak["note"]


def test_link_stats_edge_vs_base():
    # 诚实红线：跟涨率必须配基准。构造"概念天天涨"的牛市样本 → 超额≈0
    idx = [f"2026{i:04d}" for i in range(1, 81)]
    us = pd.Series([3.0] * 40 + [0.1] * 40, index=idx)     # 40个大涨日
    cn = pd.Series([1.0] * 80, index=idx)                  # 概念每天都涨
    st = link_stats(us, cn)
    assert st["hit_up"] == 100.0 and st["base_up"] == 100.0
    assert st["edge"] == 0.0                               # 看着100%·实则零超额
