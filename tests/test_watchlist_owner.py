"""自选/持仓 多归属(owner)——旧库迁移 + 分人 CRUD 单测（临时库·不动真数据）。"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from app.strategy import db as DB

_OLD_SCHEMA = """
CREATE TABLE watchlist (
    ts_code TEXT PRIMARY KEY, name TEXT, is_holding INTEGER DEFAULT 0,
    cost REAL, shares REAL, stop_loss REAL, target_price REAL, note TEXT,
    added_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp()) / "strategy.db"
    DB._get_db_path = lambda: tmp                    # type: ignore[assignment]
    return tmp


def _seed_old(tmp: Path) -> None:
    """造一个"旧版"watchlist(单列PK·无 owner)并塞两条数据，模拟线上现状。"""
    con = sqlite3.connect(str(tmp))
    con.executescript(_OLD_SCHEMA)
    con.execute("INSERT INTO watchlist (ts_code, name, is_holding, cost) VALUES (?,?,?,?)",
                ("002409.SZ", "雅克科技", 1, 50.0))
    con.execute("INSERT INTO watchlist (ts_code, name) VALUES (?,?)", ("300308.SZ", "中际旭创"))
    con.commit()
    con.close()


def test_migration_preserves_old_data_as_me() -> None:
    tmp = _fresh_db()
    _seed_old(tmp)
    DB.init_db()                                     # 触发 owner 迁移
    rows = DB.get_watchlist()
    assert len(rows) == 2
    assert all(r["owner"] == "me" for r in rows)     # 老数据全部归用户1
    yak = next(r for r in rows if r["ts_code"] == "002409.SZ")
    assert yak["is_holding"] == 1 and yak["cost"] == 50.0   # 字段无损


def test_same_stock_coexists_across_owners() -> None:
    tmp = _fresh_db()
    _seed_old(tmp)
    DB.init_db()
    # 同一只票加进爸爸的列表 —— 应与我的并存，互不覆盖
    DB.add_watch("002409.SZ", "雅克科技", owner="dad", is_holding=0)
    assert len(DB.get_watchlist()) == 3
    assert len(DB.get_watchlist(owner="me")) == 2
    assert len(DB.get_watchlist(owner="dad")) == 1
    assert DB.get_watchlist(owner="me")[0]["ts_code"] in ("002409.SZ", "300308.SZ")


def test_per_owner_update_and_remove_isolated() -> None:
    tmp = _fresh_db()
    _seed_old(tmp)
    DB.init_db()
    DB.add_watch("002409.SZ", "雅克科技", owner="dad")
    # 改爸爸那条不影响我那条
    assert DB.update_watch("002409.SZ", owner="dad", note="爸爸看的") is True
    assert DB.get_watchlist(owner="dad")[0]["note"] == "爸爸看的"
    assert (DB.get_watchlist(owner="me")[0].get("note") or "") == ""
    # 删爸爸那条不影响我那条
    assert DB.remove_watch("002409.SZ", owner="dad") is True
    assert len(DB.get_watchlist(owner="dad")) == 0
    assert any(r["ts_code"] == "002409.SZ" for r in DB.get_watchlist(owner="me"))


def test_idempotent_reinit_no_dup_migration() -> None:
    tmp = _fresh_db()
    _seed_old(tmp)
    DB.init_db()
    DB.init_db()                                     # 二次 init 不应再迁移/不报错/不丢数据
    assert len(DB.get_watchlist()) == 2


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_watchlist_owner 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
