"""宏观面板 · 计算层纯函数测试。

四条用户定档的设计逐一守住：
① 统计只在新值序列上算（月频 ffill 不得压扁分布）
② mark 断点分位照常显示，score_from 只管评分（两个维度，不是一个分支）
③ 分层评分分母动态（跳过的指标从 Σweight 同步剔除）
④ direction 翻转在分位上做（100−pctile），得分恒在 [0,100]

运行：.venv/bin/python tests/test_macro_compute.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.macro.compute import (ANOMALY, anomaly_flags, changes,       # noqa: E402
                               layer_scores, rolling_stats)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _days(n: int, start: str = "2023-01-01") -> pd.Index:
    return pd.Index(pd.bdate_range(start, periods=n).strftime("%Y%m%d"))


# ── point-in-time：分位只用当日及以前 ─────────────────────────────────────
def test_pctile_point_in_time() -> None:
    idx = _days(300)
    s = pd.Series(np.arange(300, dtype=float), index=idx)
    st = rolling_stats(s, "daily", [], "mark")
    _assert(pd.isna(st["pctile"].iloc[248]), "第249个点样本不足250→分位留空(不短窗凑数)")
    _assert(st["pctile"].iloc[249] == 100.0, "单调升序列·第250点应为其窗口内最大=100分位")
    _assert(int(st["sample_n"].iloc[249]) == 250, "sample_n 应=窗口内有效样本数")
    st2 = rolling_stats(pd.concat([s, pd.Series([9999.0], index=_days(1, "2024-03-01"))]),
                        "daily", [], "mark")
    _assert(st["pctile"].iloc[260] == st2["pctile"].iloc[260],
            "⭐追加未来数据后·历史分位必须一字不变(右端=当前行·无前视)")
    print("  ✓ 分位 point-in-time：不足250留空·追加未来不改历史")


# ── truncate 分段：语义断点前后绝不混窗 ────────────────────────────────────
def test_truncate_segmentation() -> None:
    idx = _days(600)
    brk = str(idx[300])
    # 确定性构造：断点前 1000~1300(高量级)，断点后 10~20 单调升(如北向净买入→成交额)
    s = pd.Series(np.r_[np.linspace(1000, 1300, 300), np.linspace(10, 20, 300)], index=idx)
    st = rolling_stats(s, "daily", [brk], "truncate")
    _assert(int(st["sample_n"].iloc[300]) == 1, "断点首日 sample_n 重置为1(绝不带入断点前样本)")
    _assert(pd.isna(st["pctile"].iloc[300 + 248]), "断点后第249天样本仍不足250→留空")
    p = st["pctile"].iloc[300 + 260]
    _assert(p == 100.0, f"段内单调升·当前值=段内最大→分位应=100·实得{p}"
                        "（若混入断点前的1000量级·10~20只能排在最底部）")
    mk = rolling_stats(s, "daily", [brk], "mark")
    _assert(int(mk["sample_n"].iloc[300 + 260]) > 500,
            "②mark 模式必须全窗口照算(分位正常显示·评分门禁由 score_from 负责)")
    _assert(mk["pctile"].iloc[300 + 260] < 50,
            "mark 全窗口下·10~20 混在 1000 量级里必然排在下半区(与 truncate=100 形成对照)")
    print("  ✓ 断点：truncate 分段重置·mark 全窗口(两个维度不混)")


# ── 变动口径：利率absolute·价格pct·月频无chg_5d ────────────────────────────
def test_changes_units() -> None:
    idx = _days(6)
    rate = pd.Series([1.50, 1.60, 1.55, 1.70, 1.65, 1.80], index=idx)
    c1, _ = changes(rate, "%", "daily")
    _assert(abs(c1.iloc[1] - 0.10) < 1e-9, "利率(%)用绝对差=百分点")
    px = pd.Series([100.0, 110.0, 99.0, 105.0, 105.0, 210.0], index=idx)
    c1p, c5p = changes(px, "", "daily")
    _assert(abs(c1p.iloc[1] - 10.0) < 1e-9, "价格用涨跌幅%")
    _assert(abs(c5p.iloc[5] - 110.0) < 1e-9, "chg_5d=5期涨跌幅")
    _, c5m = changes(rate, "%", "monthly")
    _assert(c5m.isna().all(), "月频 chg_5d 无意义·留空")
    print("  ✓ 变动口径：利率pp·价格%·月频无5期")


# ── 异动：OR组合(V3档·经真实频率研究) + 日频连续2日同向确认 ──────────────────
def test_anomaly_confirmation() -> None:
    rng = np.random.default_rng(42)
    n = 400
    idx = _days(n)
    chg = pd.Series(rng.normal(0, 1, n), index=idx)
    chg.iloc[-3] = 8.0                                     # 单日尖峰
    chg.iloc[-2], chg.iloc[-1] = 7.0, 7.5                  # 连续两日同向大变动
    z = pd.Series(rng.normal(0, 0.5, n), index=idx)
    z.iloc[-3:] = [3.0, 3.2, 3.1]
    flags = anomaly_flags(chg, z, "daily")
    _assert(flags.iloc[-3] == 0, "⭐单日尖峰(前一日未触发)不报——日频序列尾部厚·滤单日噪音")
    _assert(flags.iloc[-1] == 1, "连续2日同向触发→第2日起报·方向=+1")
    down = anomaly_flags(-chg, -z, "daily")
    _assert(down.iloc[-1] == -1, "向下异动方向=-1")
    monthly = anomaly_flags(chg.iloc[-30:], z.iloc[-30:], "monthly",
                            {**ANOMALY, "chg_win": 24, "chg_min": 12})
    _assert(monthly.iloc[-1] != 0, "月频=发布事件·单次触发即报(无'连续'可言)")
    print("  ✓ 异动：OR组合·日频2日确认·月频单发·方向±1")


# ── 分层评分：动态分母 + 分位翻转 + score_from + stale 规则 ─────────────────
_META = {
    "a": {"direction": 1, "weight": 1.0, "layer": "L0_liquidity", "freq": "daily", "score_from": ""},
    "b": {"direction": -1, "weight": 1.0, "layer": "L0_liquidity", "freq": "daily", "score_from": ""},
    "c": {"direction": 1, "weight": 2.0, "layer": "L0_liquidity", "freq": "daily", "score_from": "20270101"},
    "d": {"direction": 0, "weight": 1.0, "layer": "L0_liquidity", "freq": "daily", "score_from": ""},
    "m": {"direction": 1, "weight": 1.0, "layer": "L0_liquidity", "freq": "monthly", "score_from": ""},
}


def _panel(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["trade_date", "code", "value", "pctile", "is_stale"])


def test_layer_score_direction_and_denominator() -> None:
    d = "20260731"
    panel = _panel([
        (d, "a", 1.0, 80.0, 0),      # 利多·分位80 → adj 80
        (d, "b", 1.0, 80.0, 0),      # 利空·分位80 → adj 20（④翻转在分位上做）
        (d, "c", 1.0, 90.0, 0),      # score_from 未到 → 只展示不评分（权重2一并剔除）
        (d, "d", 1.0, 99.0, 0),      # direction=0 → 永不评分
        (d, "m", 1.0, 60.0, 20),     # 月频沿用20会话 → 固有节奏·照常评分
    ])
    out = layer_scores(panel, _META)
    l0 = out[(out["layer"] == "L0_liquidity")].iloc[0]
    _assert(abs(l0["score"] - (80 + 20 + 60) / 3) < 0.01,   # 落库前圆整2位
            f"③动态分母：c(权重2)与d被剔除·得分=(80+20+60)/3≈53.33·实得{l0['score']}"
            "——若分母仍含c的权重2·得分会被系统性拉低到32")
    _assert(l0["n_part"] == 3 and l0["n_total"] == 4,
            f"参与3项/可评分全集4项(a,b,c,m·d方向0不算)·实得{l0['n_part']}/{l0['n_total']}")
    total = out[out["layer"] == "TOTAL"].iloc[0]
    _assert(total["score"] == l0["score"], "当前仅L0有数据·总分=L0")
    print("  ✓ 评分：分位翻转·动态分母·score_from只管评分·月频沿用照常计分")


def test_layer_score_stale_rules() -> None:
    d = "20260731"
    panel = _panel([
        (d, "a", 1.0, 80.0, 1),      # daily 沿用1会话(外盘隔夜) → 正常参与
        (d, "b", 1.0, 80.0, 2),      # daily 沿用2会话 → 源降级·只展示不评分
    ])
    out = layer_scores(panel, _META)
    l0 = out[(out["layer"] == "L0_liquidity")].iloc[0]
    _assert(abs(l0["score"] - 80.0) < 0.01 and l0["n_part"] == 1,
            "daily: 1会话参与(80)·2会话剔除——阈值与'外盘隔夜差1属正常'一致")
    print("  ✓ 评分：daily沿用≤1参与·≥2剔除")


def test_layer_score_bounds_random() -> None:
    """随机 500 天×5指标·得分必须恒在 [0,100]（④若用 ×-1 翻转·此测必炸出负分）。"""
    rng = np.random.default_rng(1)
    days = [str(d) for d in _days(500)]
    rows = []
    for d in days:
        for c in _META:
            rows.append((d, c, 1.0, float(rng.uniform(0, 100)), int(rng.integers(0, 3))))
    out = layer_scores(_panel(rows), _META)
    s = out["score"].dropna()
    _assert(len(s) > 0 and s.min() >= 0 and s.max() <= 100,
            f"得分越界[{s.min()},{s.max()}]——direction翻转实现有误")
    print(f"  ✓ 评分边界：随机500天·min={s.min():.1f} max={s.max():.1f}·恒在[0,100]")


if __name__ == "__main__":
    print("宏观面板 · 计算层测试")
    for fn in (test_pctile_point_in_time, test_truncate_segmentation, test_changes_units,
               test_anomaly_confirmation, test_layer_score_direction_and_denominator,
               test_layer_score_stale_rules, test_layer_score_bounds_random):
        fn()
    print("✅ 全部通过")
