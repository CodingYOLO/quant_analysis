"""
板块广度雷达 sector_radar 单测：催化理由 + 三栏组装(注入假宽表) + 成分广度时序(假Provider)。

零依赖，可直接运行：python -m tests.test_sector_radar
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

import app.strategy.sector_radar as R


# ── 催化理由（纯函数）───────────────────────────────────────────────────────
def test_reasons_carry_numbers_and_units() -> None:
    row = {"money_flow_5d": 104.2, "money_flow_3d": 60.0, "breadth_ma20": 90.5,
           "pct_chg_1d": -1.08, "pct_chg_3d": 2.1, "pct_chg_5d": 12.8, "top100_ratio": 25.0}
    dip = R._dip_reason(row)
    assert "+104亿" in dip and "90%" in dip and "-1.1%" in dip and "低吸" in dip
    assert "+60亿" in R._rotate_reason(row) and "在途主线" in R._rotate_reason(row)
    assert "+12.8%" in R._risk_reason(row) and "防接盘" in R._risk_reason(row)
    # None 安全 → '—'，不抛错
    assert "—" in R._dip_reason({"breadth_ma20": None})


def test_bucket_row_and_brief() -> None:
    r = {"theme_name": "银行", "heat_score": 70.0, "breadth_ma20": 90.5,
         "money_flow_5d": 104.2, "pct_chg_1d": -1.08, "phase": "趋势",
         "signal": "低吸", "signals": ["低吸"]}
    br = R._bucket_row(r, R._dip_reason)
    assert br["name"] == "银行" and br["signals"] == ["低吸"] and "低吸" in br["reason"]
    bf = R._board_brief(r)
    assert bf["name"] == "银行" and bf["phase"] == "趋势" and bf["signal"] == "低吸"


# ── 三栏雷达（注入假宽表）────────────────────────────────────────────────────
def _fake_scope(available=True, dip=True):
    rows = [{"theme_name": "PCB概念", "heat_score": 90.0, "phase": "趋势",
             "breadth_ma20": 63.6, "signal": "高位风险", "signals": ["高位风险"],
             "money_flow_5d": 130.0, "money_flow_3d": 80.0, "pct_chg_3d": 5.0,
             "pct_chg_5d": 10.6, "pct_chg_1d": 7.6, "top100_ratio": 20.0},
            {"theme_name": "银行", "heat_score": 55.0, "phase": "震荡",
             "breadth_ma20": 90.5, "signal": "低吸", "signals": ["低吸"],
             "money_flow_5d": 104.0, "money_flow_3d": 30.0, "pct_chg_3d": 0.5,
             "pct_chg_5d": 1.0, "pct_chg_1d": -1.1, "top100_ratio": 5.0}]
    buckets = {"dip": [rows[1]] if dip else [], "rotate": [rows[1]], "risk": [rows[0]]}
    return {"available": available, "date": "20260615", "rows": rows, "buckets": buckets}


def test_build_radar_with_dip(monkeypatch=None) -> None:
    import app.strategy.sector_scope as SS
    orig = SS.build_sectorscope
    SS.build_sectorscope = lambda date="", theme_types=(): _fake_scope(dip=True)
    try:
        r = R.build_sector_radar(theme_type="concept")
        assert r["ok"] and r["available"] and r["date"] == "20260615"
        assert len(r["boards"]) == 2 and len(r["dip"]) == 1 and r["dip"][0]["name"] == "银行"
        assert r["default"] == "银行"                 # 有低吸 → 默认选低吸首位
        assert "宁可不抄" in r["note"]                 # 诚实提示常驻
        assert r["risk"][0]["name"] == "PCB概念"
    finally:
        SS.build_sectorscope = orig


def test_build_radar_no_dip_falls_back_to_top_heat() -> None:
    import app.strategy.sector_scope as SS
    orig = SS.build_sectorscope
    SS.build_sectorscope = lambda date="", theme_types=(): _fake_scope(dip=False)
    try:
        r = R.build_sector_radar(theme_type="concept")
        assert r["dip"] == [] and r["default"] == "PCB概念"   # 无低吸 → 退回热度首位
    finally:
        SS.build_sectorscope = orig


def test_build_radar_unavailable() -> None:
    import app.strategy.sector_scope as SS
    orig = SS.build_sectorscope
    SS.build_sectorscope = lambda date="", theme_types=(): {"available": False, "date": "", "msg": "宽表未计算"}
    try:
        r = R.build_sector_radar()
        assert r["ok"] and r["available"] is False
    finally:
        SS.build_sectorscope = orig


# ── 成分广度时序（假 Provider）───────────────────────────────────────────────
class _FakeProvider:
    """3 只「测试业」成分：2 只持续上涨(站上MA)、1 只持续下跌 → 广度 ~66%。"""
    def __init__(self, n: int = 90):
        self._dates = [f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)]
        self._n = n

    def get_stock_basic(self):
        return pd.DataFrame({"ts_code": ["A.SZ", "B.SZ", "C.SZ", "Z.SZ"],
                             "name": ["甲", "乙", "丙", "它"],
                             "industry": ["测试业", "测试业", "测试业", "别的业"]})

    def get_daily_basic(self, date):
        return pd.DataFrame({"ts_code": ["A.SZ", "B.SZ", "C.SZ"],
                             "circ_mv": [300.0, 200.0, 100.0]})

    def get_trade_cal(self, start, end):
        return pd.DataFrame({"cal_date": self._dates, "is_open": [1] * self._n})

    def get_stock_daily(self, ts_code, start, end):
        up = ts_code in ("A.SZ", "B.SZ")
        base = [100 + i * 0.5 for i in range(self._n)] if up else [200 - i * 0.5 for i in range(self._n)]
        return pd.DataFrame({"trade_date": self._dates,
                             "open": base, "high": [x + 1 for x in base],
                             "low": [x - 1 for x in base], "close": base,
                             "vol": [1000] * self._n, "amount": [1e5] * self._n,
                             "pct_chg": [0.5 if up else -0.5] * self._n})

    def get_adj_factor_series(self, ts_code, start, end):
        return pd.DataFrame({"trade_date": self._dates, "adj_factor": [1.0] * self._n})


def test_get_board_members_industry() -> None:
    m = R.get_board_members(_FakeProvider(), "测试业", "industry")
    assert set(m) == {"A.SZ", "B.SZ", "C.SZ"}      # 仅本行业，剔除别的业


def test_compute_board_breadth() -> None:
    tmp = Path(tempfile.mkdtemp())
    R._cache_file = lambda *a, **k: tmp / "x.json"   # type: ignore[assignment]
    out = R.compute_board_breadth("测试业", "industry", days=30,
                                  provider=_FakeProvider(), max_members=10)
    assert out["ok"] and out["n_members"] == 3
    assert out["curve"] and all("ma20" in c for c in out["curve"])
    last = out["curve"][-1]
    # 2 涨 1 跌 → 站上 MA20 占比约 66%（>50，结构偏健康）
    assert last["ma20"] is not None and 60 <= last["ma20"] <= 70
    # 命中缓存：第二次直接读文件，不重算（返回同结构）
    out2 = R.compute_board_breadth("测试业", "industry", days=30,
                                   provider=_FakeProvider(), max_members=10)
    assert out2["curve"][-1]["ma20"] == last["ma20"]


def test_compute_board_breadth_no_members() -> None:
    tmp = Path(tempfile.mkdtemp())
    R._cache_file = lambda *a, **k: tmp / "y.json"   # type: ignore[assignment]
    out = R.compute_board_breadth("不存在业", "industry", provider=_FakeProvider())
    assert out["ok"] is False and "成分" in out["msg"]


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
