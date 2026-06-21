"""策略适配扫描(scout)落历史 record_scout 单测。

用临时 DB（monkeypatch _db_path），零副作用。直接运行：python -m tests.test_history_scout
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import app.backtest.history as H


def _use_temp_db():
    """把历史 DB 指向临时文件（路径固定，多次调用返回同一库），隔离真实缓存库。"""
    path = Path(tempfile.mkdtemp()) / "bt_hist.db"
    H._db_path = lambda: path


def _scout_result() -> dict:
    return {
        "ok": True, "ts_code": "300308.SZ", "start": "20260301", "end": "20260618",
        "n_total": 16, "n_eligible": 1,
        "ranked": [
            {"key": "ema_bull", "label": "EMA多头", "n": 56, "win_rate": 0.77, "avg_return": 6.84, "profit_factor": 2.1, "tier": "rec"},
            {"key": "macd", "label": "MACD金叉", "n": 10, "win_rate": 0.40, "avg_return": -1.0, "profit_factor": 0.8, "tier": "neg"},
        ],
        "recommended": ["ema_bull"],
    }


def test_record_scout_and_list() -> None:
    _use_temp_db()
    hid = H.record_scout("u1", _scout_result(), name="中际旭创")
    assert hid > 0
    rows = H.list_records(creator="u1")
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "scout" and r["signal_key"] == "__scout__"
    assert "EMA多头" in r["signal_label"]            # 头条取最高分推荐打法
    assert r["win_rate"] == 0.77 and r["head_horizon"] == 5 and r["n_signals"] == 56


def test_get_record_returns_full_blob() -> None:
    _use_temp_db()
    hid = H.record_scout("u1", _scout_result(), name="中际旭创")
    rec = H.get_record(hid, creator="u1")
    assert rec["kind"] == "scout"
    assert rec["result"]["ts_code"] == "300308.SZ" and len(rec["result"]["ranked"]) == 2


def test_record_scout_overwrites_same_window() -> None:
    _use_temp_db()
    h1 = H.record_scout("u1", _scout_result(), name="x")
    h2 = H.record_scout("u1", _scout_result(), name="x")     # 同票同窗口 → 覆盖刷新
    assert h1 == h2
    assert len(H.list_records(creator="u1")) == 1


def test_headline_falls_back_when_top_not_recommended() -> None:
    """最高分打法未被推荐时，头条回退取第一个推荐打法。"""
    _use_temp_db()
    res = _scout_result()
    res["ranked"][0]["tier"] = "neg"     # 顶部不在推荐内（recommended 仍只含 ema_bull）
    res["recommended"] = ["macd"]
    hid = H.record_scout("u1", res, name="x")
    rows = H.list_records(creator="u1")
    assert "MACD金叉" in rows[0]["signal_label"]


def test_record_scout_empty_ts_safe() -> None:
    _use_temp_db()
    assert H.record_scout("u1", {"ranked": []}) == 0


# ---- 个股360 快照 record_stock360 + kinds 过滤隔离 ----

def _s360_snapshot() -> dict:
    return {"code": "300308.SZ", "name": "中际旭创",
            "verdict": {"stance": "观望", "score": 70, "summary": "高位背离"},
            "profile": {"ok": True}, "fund": {"ok": True}}


def test_record_stock360_and_label() -> None:
    _use_temp_db()
    hid = H.record_stock360("u1", _s360_snapshot(), name="中际旭创")
    assert hid > 0
    rows = H.list_records(creator="u1", kinds=("stock360",))
    assert len(rows) == 1 and rows[0]["kind"] == "stock360"
    assert "观望" in rows[0]["signal_label"] and "70" in rows[0]["signal_label"]
    rec = H.get_record(hid, creator="u1")
    assert rec["result"]["verdict"]["score"] == 70 and rec["result"]["code"] == "300308.SZ"


def test_kinds_filter_isolation() -> None:
    """回测/scout/360 三类共存一表，kinds 过滤互不串台。"""
    _use_temp_db()
    H.record_scout("u1", _scout_result(), name="中际旭创")
    H.record_stock360("u1", _s360_snapshot(), name="中际旭创")
    assert len(H.list_records(creator="u1", kinds=("scout",))) == 1
    assert len(H.list_records(creator="u1", kinds=("stock360",))) == 1
    assert len(H.list_records(creator="u1", kinds=("backtest", "scout"))) == 1   # 不含360
    assert len(H.list_records(creator="u1")) == 2                                 # 无过滤=全部


def test_record_stock360_empty_code_safe() -> None:
    _use_temp_db()
    assert H.record_stock360("u1", {"verdict": {}}) == 0


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_history_scout 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
