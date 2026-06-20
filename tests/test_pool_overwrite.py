"""
选股池 save_pool 真覆盖单测：改选/换口径后旧股不残留（守 db.save_pool 的"覆盖写入"承诺）。

零依赖（monkeypatch 临时库），可直接运行：python -m tests.test_pool_overwrite
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import app.strategy.db as DB


def test_save_pool_overwrites_clean() -> None:
    tmp = Path(tempfile.mkdtemp()) / "strategy.db"
    DB._get_db_path = lambda: tmp                   # type: ignore[assignment]

    DB.save_pool("20260101", [{"ts_code": "A.SZ", "theme": "半导体"},
                              {"ts_code": "B.SZ", "theme": "元器件"},   # 旧 Tushare 口径
                              {"ts_code": "C.SZ", "theme": "专用机械"}])
    # 改选：C 落选、B 换成申万二级口径 → 旧池不应残留 C、B 的旧值不应滞留
    DB.save_pool("20260101", [{"ts_code": "A.SZ", "theme": "半导体"},
                              {"ts_code": "B.SZ", "theme": "元件"}])

    con = sqlite3.connect(str(tmp))
    rows = dict(con.execute(
        "SELECT ts_code, theme FROM stock_pool WHERE run_date=?", ("20260101",)).fetchall())
    assert set(rows) == {"A.SZ", "B.SZ"}           # C 已被清，无陈留
    assert rows["B.SZ"] == "元件"                   # 旧口径被新值覆盖


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
