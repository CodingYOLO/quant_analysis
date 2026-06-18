"""
历史主题胜率前向闸门 _apply_winrate_gate 单元测试（对标吴川低胜率禁追涨）。

零依赖，可直接运行：python -m tests.test_winrate_gate
"""

from __future__ import annotations

from app.strategy.stock_pool import (
    _apply_winrate_gate, _WINRATE_MIN_SAMPLES, _WINRATE_VETO, _WINRATE_WARN,
)


def _rec(theme: str, conf: float = 0.7) -> dict:
    return {"theme": theme, "confidence": conf, "risk_flags": []}


def test_low_winrate_veto_and_demote() -> None:
    wr = {"普钢": {"win_rate": 0.20, "samples": 30, "avg_return": -0.3}}
    rec = _rec("普钢")
    assert _apply_winrate_gate(rec, wr) is True            # 降级
    assert any("避免追涨" in f for f in rec["risk_flags"])
    assert rec["confidence"] <= 0.45                        # 置信度被压低


def test_mid_winrate_warn_only() -> None:
    wr = {"水运": {"win_rate": 0.42, "samples": 15, "avg_return": 0.1}}
    rec = _rec("水运", conf=0.7)
    assert _apply_winrate_gate(rec, wr) is False           # 不降级
    assert any("偏低" in f for f in rec["risk_flags"])
    assert rec["confidence"] == 0.7                          # 置信度不变


def test_small_sample_not_judged() -> None:
    # 样本不足阈值 → 不判（避免小样本误杀）
    wr = {"铅锌": {"win_rate": 0.10, "samples": _WINRATE_MIN_SAMPLES - 1, "avg_return": 0}}
    rec = _rec("铅锌")
    assert _apply_winrate_gate(rec, wr) is False
    assert rec["risk_flags"] == []


def test_high_winrate_passes_clean() -> None:
    wr = {"元器件": {"win_rate": 0.62, "samples": 21, "avg_return": 1.6}}
    rec = _rec("元器件")
    assert _apply_winrate_gate(rec, wr) is False
    assert rec["risk_flags"] == []


def test_unknown_theme_passes() -> None:
    rec = _rec("没记录的主题")
    assert _apply_winrate_gate(rec, {}) is False
    assert rec["risk_flags"] == []


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
