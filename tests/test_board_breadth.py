"""
板块广度预算 board_breadth 单测：面板→广度时序(纯函数) + 预算填充 + 缓存往返。

零依赖，可直接运行：python -m tests.test_board_breadth
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

import app.factors.board_breadth as B


def _panel(n: int = 40) -> pd.DataFrame:
    """全市场面板(index=ts_code, columns=日期升序)：A/B 持续上涨、C 持续下跌。"""
    dates = [f"202604{i + 1:02d}" if i < 30 else f"202605{i - 29:02d}" for i in range(n)]
    rows = {
        "A.SZ": [100 + i for i in range(n)],
        "B.SZ": [120 + i for i in range(n)],
        "C.SZ": [200 - i for i in range(n)],
    }
    return pd.DataFrame(rows, index=dates).T   # index=ts_code, columns=dates


# ── 纯函数：面板 → 广度时序 ──────────────────────────────────────────────────
def test_breadth_series_from_panel() -> None:
    panel = _panel()
    curve = B.breadth_series_from_panel(panel, ["A.SZ", "B.SZ", "C.SZ"])
    assert curve and all({"date", "ma5", "ma20"} <= set(p) for p in curve)
    last = curve[-1]
    # 2 涨 1 跌 → 站上 MA20 占比 ≈ 66.7%（A、B 在 MA 上方，C 在下方）
    assert last["ma20"] is not None and 60 <= last["ma20"] <= 70
    # 升序：日期递增
    assert [p["date"] for p in curve] == sorted(p["date"] for p in curve)


def test_breadth_too_few_members() -> None:
    panel = _panel()
    assert B.breadth_series_from_panel(panel, ["A.SZ", "B.SZ"]) == []   # <3 不算
    assert B.breadth_series_from_panel(panel, ["NONE.SZ"]) == []         # 不在面板


def test_fill_skips_thin_and_keeps_members() -> None:
    panel = _panel()
    out: dict = {}
    B._fill(out, "industry", {"半导体": ["A.SZ", "B.SZ", "C.SZ"], "空板块": ["X.SZ"]}, panel)
    assert "industry::半导体" in out and out["industry::半导体"]["n_members"] == 3
    assert "industry::空板块" not in out          # 成分不足被跳过


# ── 缓存往返 ────────────────────────────────────────────────────────────────
def test_cache_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp())
    B._cache_dir = lambda: tmp                     # type: ignore[assignment]
    curve = [{"date": "20260601", "ma5": 80.0, "ma20": 66.7},
             {"date": "20260602", "ma5": 70.0, "ma20": 60.0}]
    (tmp / "20260602.json").write_text(
        json.dumps({"concept::CPO": {"n_members": 30, "curve": curve}}, ensure_ascii=False),
        encoding="utf-8")
    out = B.load_cached_breadth("concept", "CPO", days=45)
    assert out and out["ok"] and out["cached"] and out["n_members"] == 30
    assert out["current"]["ma20"] == 60.0 and out["end"] == "20260602"
    # days 裁剪：只要最近 1 个点
    assert len(B.load_cached_breadth("concept", "CPO", days=1)["curve"]) == 1
    # 不存在的板块 → None（端点回退实时）
    assert B.load_cached_breadth("concept", "不存在", 45) is None
    assert B.load_cached_breadth("industry", "CPO", 45) is None   # 类型不匹配


def test_load_no_cache_returns_none() -> None:
    B._cache_dir = lambda: Path(tempfile.mkdtemp())   # type: ignore[assignment]
    assert B.load_cached_breadth("concept", "CPO", 45) is None    # 空目录


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
