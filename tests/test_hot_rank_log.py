"""人气榜自建轨迹·DB层测试：临时库往返(记多日→聚合峰值/谷值/当前) + 纯聚合直测。

运行：.venv/bin/python tests/test_hot_rank_log.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy import db  # noqa: E402
from app.strategy.db import _agg_hot_trajectory  # noqa: E402


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


def test_agg_pure() -> None:
    rows = [
        {"code": "000001", "name": "A", "rank": 30, "trade_date": "20260601"},
        {"code": "000001", "name": "A", "rank": 500, "trade_date": "20260610"},
        {"code": "000001", "name": "A", "rank": 380, "trade_date": "20260615"},
    ]
    r = _agg_hot_trajectory(rows)[0]
    _assert(r["peak_rank"] == 30, "峰值=最小名次")
    _assert(r["trough_rank"] == 500, "谷值=最大名次")
    _assert(r["cur_rank"] == 380, "当前=最新日名次")
    _assert(r["days_seen"] == 3, "覆盖天数=3")
    print("  ✓ 纯聚合：峰值30/谷值500/当前380(最新日)/3天")


def test_agg_skips_missing() -> None:
    rows = [{"code": "x", "rank": None, "trade_date": "20260601"},
            {"code": "y", "rank": 10, "trade_date": "20260601"}]
    r = _agg_hot_trajectory(rows)
    _assert(len(r) == 1 and r[0]["code"] == "y", "缺 rank 应跳过")
    print("  ✓ 缺 rank 行跳过(边界)")


def test_roundtrip_tempdb() -> None:
    with tempfile.TemporaryDirectory() as d:
        orig = db._get_db_path
        db._get_db_path = lambda: Path(d) / "t.db"          # 指向临时库·不污染真库
        try:
            db.init_db()
            db.log_hot_rank("rank", [{"code": "000001", "name": "A", "rank": 40},
                                     {"code": "000002", "name": "B", "rank": 12}], "20260610")
            db.log_hot_rank("rank", [{"code": "000001", "name": "A", "rank": 520}], "20260612")
            n = db.log_hot_rank("rank", [{"code": "000001", "name": "A", "rank": 360}], "20260615")
            _assert(n == 1, "末日写入1条")
            traj = {t["code"]: t for t in db.hot_rank_trajectory("rank", days=14)}
            a = traj["000001"]
            _assert(a["peak_rank"] == 40 and a["trough_rank"] == 520 and a["cur_rank"] == 360,
                    f"轨迹聚合错 {a}")
            _assert(traj["000002"]["days_seen"] == 1, "B只出现1天")
            # 幂等：同日同票覆盖
            db.log_hot_rank("rank", [{"code": "000001", "name": "A", "rank": 999}], "20260615")
            a2 = {t["code"]: t for t in db.hot_rank_trajectory("rank")}["000001"]
            _assert(a2["cur_rank"] == 999, "同日覆盖生效")
        finally:
            db._get_db_path = orig
    print("  ✓ 临时库往返：多日记录→轨迹聚合正确·同日幂等覆盖")


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n人气榜自建轨迹 DB 测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
