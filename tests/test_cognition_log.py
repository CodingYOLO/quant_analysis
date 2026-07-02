"""认知脚手架·推演日志 DB 测试（临时库往返：存/取/当日覆盖/列表/回看自评）。

运行：.venv/bin/python tests/test_cognition_log.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy import db  # noqa: E402


def _assert(c: bool, m: str) -> None:
    if not c:
        raise AssertionError(m)


def test_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        orig = db._get_db_path
        db._get_db_path = lambda: Path(d) / "t.db"
        try:
            db.init_db()
            e = {"q1_regime": "结构牛市", "q2_mainline": "半导体", "q3_tempo": "情绪偏高·等回调",
                 "q4_catalyst": "涨价潮", "q5_path": "路径A科技轮动", "stance": "均衡",
                 "main_line": "半导体", "confidence": 3, "sh_close": 3450.5}
            db.save_cognition("20260702", e)
            g = db.get_cognition("20260702")
            _assert(g and g["stance"] == "均衡" and g["confidence"] == 3, f"取回错 {g}")
            _assert(g["main_line"] == "半导体" and g["sh_close"] == 3450.5, "字段错")
            # 当日再存→覆盖(不新增)
            db.save_cognition("20260702", {**e, "stance": "防守", "confidence": 2})
            g2 = db.get_cognition("20260702")
            _assert(g2["stance"] == "防守" and g2["confidence"] == 2, "当日覆盖失败")
            _assert(len(db.list_cognition()) == 1, "同日应仅1条")
            # 另一天
            db.save_cognition("20260703", {**e, "stance": "进攻"})
            lst = db.list_cognition()
            _assert(len(lst) == 2 and lst[0]["trade_date"] == "20260703", "列表倒序错")
            # 回看自评
            _assert(db.update_cognition_review("20260702", "节奏看错·情绪没那么高"), "自评应成功")
            _assert(db.get_cognition("20260702")["review_note"].startswith("节奏"), "自评未落库")
            _assert(not db.update_cognition_review("20991231", "x"), "不存在的日期应False")
            print("  ✓ 存/取/当日覆盖/倒序列表/回看自评 全通")
        finally:
            db._get_db_path = orig


def _run_all() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"\n认知脚手架 DB 测试（{len(tests)} 项）")
    for t in tests:
        t()
    print(f"\n✅ 全部 {len(tests)} 项通过\n")


if __name__ == "__main__":
    _run_all()
