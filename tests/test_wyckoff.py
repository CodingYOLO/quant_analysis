"""威科夫量价因子·纯函数测试（重点：point-in-time 无前视 + 涨跌停量能处理 + 形态边界）。

对应用户 prompt 的硬性工程要求。运行：.venv/bin/python tests/test_wyckoff.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.factors.wyckoff import (  # noqa: E402
    detect_double_top, obv_divergence, obv_series, obv_slope_norm,
    squeeze_pctile, wyckoff_phase,
)


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


def _ser(vals) -> pd.Series:
    return pd.Series([float(v) for v in vals])


# ── point-in-time：给定"截至T"的历史，函数值不受其后未来数据影响 ────────────────
def test_point_in_time_no_lookahead() -> None:
    rng = np.sin(np.linspace(0, 12, 300)) * 3 + np.linspace(20, 40, 300)
    close = _ser(rng)
    vol = _ser(np.abs(np.cos(np.linspace(0, 20, 300))) * 1e6 + 5e5)
    high, low = close + 0.5, close - 0.5
    T = 200
    # 只用 ≤T 的历史
    pre_c, pre_v, pre_h, pre_l = close[:T], vol[:T], high[:T], low[:T]
    fns = [
        ("obv_slope", lambda c, v, h, lo: obv_slope_norm(c, v, 20)),
        ("obv_div", lambda c, v, h, lo: obv_divergence(c, v, 20)),
        ("squeeze", lambda c, v, h, lo: squeeze_pctile(h, lo, c, 20, 120)),
        ("phase", lambda c, v, h, lo: wyckoff_phase(c, h, lo, v)),
    ]
    # 追加两种不同的"未来"，再切回 ≤T，结果必须与只用历史一致（证明不看未来）
    for name, fn in fns:
        base = fn(pre_c, pre_v, pre_h, pre_l)
        for fut in (999.0, -999.0):
            c2 = pd.concat([pre_c, _ser([fut] * 30)], ignore_index=True)
            v2 = pd.concat([pre_v, _ser([9e9] * 30)], ignore_index=True)
            h2 = pd.concat([pre_h, _ser([fut] * 30)], ignore_index=True)
            l2 = pd.concat([pre_l, _ser([fut] * 30)], ignore_index=True)
            got = fn(c2[:T], v2[:T], h2[:T], l2[:T])
            _assert(got == base, f"{name} 前视泄漏：截至T值随未来变化 {got}≠{base}")
    print("  ✓ point-in-time：obv斜率/背离/squeeze/阶段 均不受未来数据影响")


# ── 涨跌停量能处理：一字板量能置零、不污染 OBV ──────────────────────────────────
def test_limit_mask_zeroes_volume() -> None:
    close = _ser([10, 11, 12, 13, 14])          # 连续上涨
    vol = _ser([100, 100, 9999, 100, 100])       # 第3天为涨停一字板·量能失真(极大)
    mask = pd.Series([False, False, True, False, False])
    obv_no = obv_series(close, vol)
    obv_yes = obv_series(close, vol, mask)
    _assert(obv_no.iloc[-1] != obv_yes.iloc[-1], "涨跌停量能应被剔除后不同")
    # 首日方向=0(无前收)，涨停日(idx2)量置零 → 净贡献仅来自 idx1/3/4 上涨日各100 = 300
    _assert(obv_yes.iloc[-1] == 300.0, f"剔除涨停量后 OBV 应=300，实得 {obv_yes.iloc[-1]}")
    print("  ✓ 涨跌停：一字板量能贡献置零(不失真)")


# ── OBV 斜率：吸筹(价平·上涨日放量) → 正 ───────────────────────────────────────
def test_obv_slope_accumulation() -> None:
    # 价格窄幅震荡(基本平)，但上涨日量>下跌日量 → OBV 净上行
    close = _ser([20, 20.3, 20.1, 20.4, 20.2, 20.5, 20.3, 20.6, 20.4, 20.7,
                  20.5, 20.8, 20.6, 20.9, 20.7, 21.0, 20.8, 21.1, 20.9, 21.2])
    vol = _ser([200 if i % 2 == 1 else 80 for i in range(20)])   # 上涨日(奇)放量
    sl = obv_slope_norm(close, vol, 20)
    _assert(sl is not None and sl > 0, f"价平+上涨放量应吸筹(OBV斜率>0)，实得 {sl}")
    print(f"  ✓ OBV斜率：价平+上涨放量 → 吸筹签名(斜率{sl}>0)")


# ── OBV 背离：价创新高但 OBV 不跟 → 顶背离(<0) ─────────────────────────────────
def test_obv_divergence_top() -> None:
    # 前段稳步上行(OBV冲高)，后段震荡创新高但下跌日放量(资金流出)→ OBV 从峰值回落 = 顶背离
    close = _ser([20, 21, 22, 23, 24, 25, 26, 27, 28, 29,       # 0-9 稳步上行
                  28, 29, 28, 29, 28, 29, 28, 29, 29.5, 30])     # 10-19 震荡·末位创新高30
    vol = _ser([100] * 10 + [500, 100, 500, 100, 500, 100, 500, 100, 100, 100])  # 下跌日(偶)放量
    div = obv_divergence(close, vol, 20)
    _assert(div is not None and div < 0, f"价创新高但OBV回落应顶背离(<0)，实得 {div}")
    print(f"  ✓ OBV背离：价创新高但下跌放量致OBV回落 → 顶背离({div}<0)")


# ── Squeeze：近期波动收窄 → 低分位 ─────────────────────────────────────────────
def test_squeeze_tight() -> None:
    # 前段大幅波动，近段极窄
    wide = [20 + (5 if i % 2 else -5) for i in range(80)]
    tight = [30 + (0.1 if i % 2 else -0.1) for i in range(40)]
    close = _ser(wide + tight)
    high, low = close + 0.05, close - 0.05
    sq = squeeze_pctile(high, low, close, 20, 120)
    _assert(sq is not None and sq <= 0.3, f"近期收窄应低分位(≤0.3)，实得 {sq}")
    print(f"  ✓ Squeeze：近期波动收窄 → 低分位({sq})")


# ── 双顶破位：构造清晰双顶+跌破颈线 → True；单峰 → False ─────────────────────────
def test_double_top() -> None:
    up = [10 + i * 0.5 for i in range(20)]        # 涨到第一顶 ~19.5
    down = [19.5 - i * 0.4 for i in range(12)]     # 回落到颈线 ~15
    up2 = [15 + i * 0.45 for i in range(10)]        # 涨到第二顶 ~19.5(等高)
    brk = [19.5 - i * 0.6 for i in range(12)]       # 跌破颈线
    close = _ser(up + down + up2 + brk)
    high = close + 0.1
    vol = _ser([200] * 20 + [100] * 12 + [90] * 10 + [100] * 12)  # 第二顶量能背离(90<200)
    _assert(detect_double_top(close, high, vol, lookback=90) is True, "清晰双顶+破颈线应=True")
    # 单峰(持续上涨)→ False
    mono = _ser([10 + i * 0.3 for i in range(70)])
    _assert(detect_double_top(mono, mono + 0.1, _ser([100] * 70)) is False, "单峰不应误判双顶")
    print("  ✓ 双顶：清晰双顶破颈线=True·单峰=False")


def test_boundary() -> None:
    short = _ser([10, 11, 12])
    _assert(obv_slope_norm(short, short, 20) is None, "数据不足→None")
    _assert(squeeze_pctile(short, short, short) is None, "数据不足→None")
    _assert(wyckoff_phase(short, short, short, short) == "—", "数据不足→—")
    _assert(detect_double_top(short, short, short) is False, "数据不足→False")
    print("  ✓ 数据不足 → None/—/False（边界）")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n威科夫量价因子测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
