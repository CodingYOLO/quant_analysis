"""
个股回测历史 backtest.history 单元测试（独立临时库，零副作用）。

零依赖，可直接运行：python -m tests.test_backtest_history
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import app.backtest.history as H


def _use_temp_db() -> None:
    """把库路径指向临时文件，避免污染真实 backtest_history.db。"""
    tmp = Path(tempfile.mkdtemp()) / "backtest_history.db"
    H._db_path = lambda: tmp  # type: ignore[assignment]
    H.init()


def _mk_result(ts_code: str = "600519.SH", signal: str = "macd_gold",
               label: str = "MACD金叉", start: str = "20230101", end: str = "20231231",
               horizons: dict | None = None, n: int = 5) -> dict:
    """构造一份与 backtest_stock_signal 输出同形的结果 dict。"""
    return {
        "ts_code": ts_code, "signal": signal, "signal_label": label,
        "start": start, "end": end, "bars": 200, "n_signals": n,
        "horizons": horizons or {
            1: {"horizon": 1, "n": 5, "win_rate": 0.6, "avg_return": 1.2, "profit_factor": 1.5},
            3: {"horizon": 3, "n": 5, "win_rate": 0.5, "avg_return": 0.8, "profit_factor": 1.1},
            5: {"horizon": 5, "n": 4, "win_rate": 0.75, "avg_return": 2.0, "profit_factor": 2.2},
            10: {"horizon": 10, "n": 3, "win_rate": 0.33, "avg_return": -0.5, "profit_factor": 0.8},
        },
        "trades": [{"signal_date": "20230110", "buy_date": "20230111"}],
        "equity": [{"date": "20230111", "equity": 1.02}],
        "ok": True, "msg": "",
    }


def test_record_list_get_delete_flow() -> None:
    _use_temp_db()
    rid = H.record("admin", _mk_result(), name="贵州茅台")
    assert rid > 0

    rows = H.list_records(creator="admin")
    assert len(rows) == 1
    r = rows[0]
    assert r["ts_code"] == "600519.SH" and r["name"] == "贵州茅台"
    assert r["signal_label"] == "MACD金叉" and r["n_signals"] == 5
    # 头条优先取 T+5（有样本）
    assert r["head_horizon"] == 5 and r["win_rate"] == 0.75

    # 详情还原完整结果（json 往返后 horizons 键变字符串）
    full = H.get_record(rid, creator="admin")
    assert full is not None
    assert full["result"]["horizons"]["5"]["win_rate"] == 0.75
    assert full["result"]["n_signals"] == 5

    # 他人不可删 / 不可看
    assert H.get_record(rid, creator="someone") is None
    assert H.delete(rid, "someone") is False
    assert len(H.list_records(creator="admin")) == 1
    # 本人可删
    assert H.delete(rid, "admin") is True
    assert H.list_records(creator="admin") == []


def test_same_condition_overwrites() -> None:
    """同票·同信号·同区间·同自定义 → 覆盖更新（不新增行）。"""
    _use_temp_db()
    H.record("admin", _mk_result(), name="贵州茅台")
    # 同条件、不同结果（胜率变 0.40）→ 覆盖
    h2 = {5: {"horizon": 5, "n": 4, "win_rate": 0.40, "avg_return": -1.0, "profit_factor": 0.7}}
    H.record("admin", _mk_result(horizons=h2), name="贵州茅台")
    rows = H.list_records(creator="admin")
    assert len(rows) == 1 and rows[0]["win_rate"] == 0.40

    # 区间不同 → 视为新回测（新增行）
    H.record("admin", _mk_result(start="20240101", end="20241231"), name="贵州茅台")
    assert len(H.list_records(creator="admin")) == 2


def test_headline_fallback_when_t5_empty() -> None:
    """T+5 无样本时，头条回退到样本最多的持有期。"""
    _use_temp_db()
    horizons = {
        1: {"horizon": 1, "n": 8, "win_rate": 0.55, "avg_return": 0.9, "profit_factor": 1.3},
        5: {"horizon": 5, "n": 0, "win_rate": 0.0, "avg_return": 0.0, "profit_factor": 0.0},
    }
    H.record("admin", _mk_result(horizons=horizons))
    r = H.list_records(creator="admin")[0]
    assert r["head_horizon"] == 1 and r["win_rate"] == 0.55


def test_signal_and_custom_are_distinct() -> None:
    """技术信号模式与自定义涨跌幅模式即便同票同区间也各占一行。"""
    _use_temp_db()
    H.record("admin", _mk_result(signal="macd_gold"))
    custom_res = _mk_result(signal="", label="当日涨跌 -7~-3%")
    H.record("admin", custom_res, custom={"pct_min": -7, "pct_max": -3, "vol_mode": "any"})
    assert len(H.list_records(creator="admin")) == 2


def test_search_and_creator_isolation() -> None:
    _use_temp_db()
    H.record("admin", _mk_result(ts_code="600519.SH", label="MACD金叉"), name="贵州茅台")
    H.record("userB", _mk_result(ts_code="000001.SZ", label="KDJ金叉"), name="平安银行")
    assert len(H.list_records(creator="admin")) == 1            # 仅看本人
    assert len(H.list_records()) == 2                           # 全部
    assert len(H.list_records(q="茅台")) == 1                    # 按名搜索
    assert len(H.list_records(q="KDJ")) == 1                    # 按信号搜索


def test_invalid_result_returns_zero() -> None:
    _use_temp_db()
    assert H.record("admin", {"ts_code": ""}) == 0             # 缺 ts_code 不记录
    assert H.list_records(creator="admin") == []


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
