"""
选股策略保存 / 我的策略库（独立 SQLite，隔离于业务库）。

一条策略 = 名称 + 创建者 + 条件载荷(payload JSON：因子键/自定义条件/排序)。
仅创建者可删除自己的策略。前端「应用」时取 payload 还原选股条件。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from app.config import get_settings

_DB_FILENAME = "strategies.db"


def _db_path() -> Path:
    return get_settings().cache_dir / _DB_FILENAME


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(str(_db_path()))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init() -> None:
    """建表（幂等）。"""
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_strategies (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                creator    TEXT NOT NULL,
                payload    TEXT NOT NULL,                 -- json: {factors, customs, custom, sort_by}
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(name, creator)                     -- 同一创建者同名覆盖
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_strat_creator ON saved_strategies(creator)")


def save(name: str, creator: str, payload: dict) -> int:
    """保存/覆盖一条策略（同创建者同名覆盖），返回行 id。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("策略名称不能为空")
    init()
    blob = json.dumps(payload, ensure_ascii=False)
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO saved_strategies (name, creator, payload) VALUES (?, ?, ?)
            ON CONFLICT(name, creator) DO UPDATE SET
                payload=excluded.payload, created_at=datetime('now','localtime')
            """,
            (name, creator, blob),
        )
        row = con.execute(
            "SELECT id FROM saved_strategies WHERE name=? AND creator=?", (name, creator)
        ).fetchone()
    return int(row["id"]) if row else cur.lastrowid


def list_strategies(creator: str | None = None, q: str = "") -> list[dict]:
    """
    列出策略（按时间倒序）。creator 非空时仅看该创建者；q 模糊匹配名称/创建者。
    """
    init()
    where, params = [], []
    if creator:
        where.append("creator = ?")
        params.append(creator)
    if q:
        where.append("(name LIKE ? OR creator LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    sql = "SELECT * FROM saved_strategies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC"
    with _conn() as con:
        rows = con.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:
            d["payload"] = {}
        out.append(d)
    return out


def delete(strategy_id: int, creator: str) -> bool:
    """删除策略（仅创建者本人可删）。返回是否删除成功。"""
    init()
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM saved_strategies WHERE id=? AND creator=?", (strategy_id, creator)
        )
    return cur.rowcount > 0
