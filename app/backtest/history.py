"""
个股回测历史记录（独立 SQLite，隔离于业务库）。

一条记录 = 回测条件（票/信号或自定义/区间）+ 头条结果（信号数·T+N胜率/均收益/盈亏比）
          + 完整结果 JSON（点进去可还原整页，无需重算）+ 回测时间。

设计要点：
- 完全相同的回测（同票·同信号·同区间·同自定义条件）覆盖更新时间，避免反复试验刷屏。
- 列表查询不带完整结果 blob，保持轻量；详情单独取。
- 仅记录创建者本人可见/可删。
- `_db_path` 可被测试替换（依赖注入），零副作用。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from app.config import get_settings

_DB_FILENAME = "backtest_history.db"

# 头条持有期：优先用 T+5（与资金曲线/胜负判定口径一致），无样本时回退到样本最多的持有期。
_HEADLINE_HORIZON = 5

# 列表展示的列（不含 result 大字段）。
_LIST_COLS = (
    "id, creator, ts_code, name, signal_key, signal_label, start, end, "
    "custom, n_signals, head_horizon, win_rate, avg_return, profit_factor, created_at"
)


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
            CREATE TABLE IF NOT EXISTS backtest_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                creator       TEXT NOT NULL,
                ts_code       TEXT NOT NULL,
                name          TEXT DEFAULT '',
                signal_key    TEXT DEFAULT '',     -- 技术信号 key；自定义涨跌幅模式为 ''
                signal_label  TEXT DEFAULT '',     -- 信号/条件可读名
                start         TEXT DEFAULT '',
                end           TEXT DEFAULT '',
                custom        TEXT DEFAULT '',     -- 自定义条件 json；技术信号模式为 ''
                n_signals     INTEGER DEFAULT 0,
                head_horizon  INTEGER DEFAULT 0,   -- 头条胜率对应的持有期 T+N
                win_rate      REAL,                -- 头条胜率（0~1）
                avg_return    REAL,                -- 头条均收益（%）
                profit_factor REAL,
                result        TEXT NOT NULL,       -- 完整回测结果 json
                brief         TEXT,                -- AI 综合研判 json（生成后回填，避免重算重花钱）
                sector        TEXT,                -- 同类/板块分析 json（同上）
                created_at    TEXT DEFAULT (datetime('now','localtime')),
                -- 完全相同的回测覆盖（刷新时间），避免反复试验刷屏
                UNIQUE(creator, ts_code, signal_key, start, end, custom)
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_bthist_creator ON backtest_history(creator)"
        )
        for col in ("brief", "sector"):     # 旧库补列（幂等迁移）
            try:
                con.execute(f"ALTER TABLE backtest_history ADD COLUMN {col} TEXT")
            except Exception:
                pass


# ──────────────────────────────────────────────
# 写入
# ──────────────────────────────────────────────

def record(creator: str, result: dict[str, Any], name: str = "",
           custom: dict | None = None) -> int:
    """
    记录一次回测（同条件覆盖、刷新时间），返回行 id。

    Args:
        creator: 记录归属用户。
        result:  backtest_stock_signal 返回的完整结果 dict。
        name:    股票名称（可空）。
        custom:  自定义涨跌幅条件 dict（技术信号模式传 None）。

    Returns:
        行 id；result 非法（缺 ts_code）时返回 0。
    """
    ts_code = str(result.get("ts_code") or "").strip()
    if not ts_code:
        return 0

    head_h, head = _headline(result.get("horizons") or {})
    custom_str = json.dumps(custom, ensure_ascii=False, sort_keys=True) if custom else ""

    init()
    blob = json.dumps(result, ensure_ascii=False)
    with _conn() as con:
        con.execute(
            """
            INSERT INTO backtest_history
                (creator, ts_code, name, signal_key, signal_label, start, end, custom,
                 n_signals, head_horizon, win_rate, avg_return, profit_factor, result)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(creator, ts_code, signal_key, start, end, custom) DO UPDATE SET
                name=excluded.name, signal_label=excluded.signal_label,
                n_signals=excluded.n_signals, head_horizon=excluded.head_horizon,
                win_rate=excluded.win_rate, avg_return=excluded.avg_return,
                profit_factor=excluded.profit_factor, result=excluded.result,
                created_at=datetime('now','localtime')
            """,
            (creator, ts_code, name, str(result.get("signal") or ""),
             str(result.get("signal_label") or ""), str(result.get("start") or ""),
             str(result.get("end") or ""), custom_str, int(result.get("n_signals") or 0),
             head_h, head.get("win_rate"), head.get("avg_return"), head.get("profit_factor"),
             blob),
        )
        row = con.execute(
            "SELECT id FROM backtest_history WHERE creator=? AND ts_code=? AND "
            "signal_key=? AND start=? AND end=? AND custom=?",
            (creator, ts_code, str(result.get("signal") or ""),
             str(result.get("start") or ""), str(result.get("end") or ""), custom_str),
        ).fetchone()
    return int(row["id"]) if row else 0


def _headline(horizons: dict) -> tuple[int, dict]:
    """
    从各持有期统计中挑选头条：优先 T+5（有样本），否则取样本数最多者。

    horizons 的键可能是 int（内存结果）或 str（json 还原），统一兼容。
    Returns: (持有期N, 该持有期统计 dict)；无任何样本返回 (0, {})。
    """
    def _get(h: int) -> dict | None:
        return horizons.get(h) or horizons.get(str(h))

    pref = _get(_HEADLINE_HORIZON)
    if pref and int(pref.get("n") or 0) > 0:
        return _HEADLINE_HORIZON, pref

    best_h, best = 0, {}
    for h, stat in horizons.items():
        n = int((stat or {}).get("n") or 0)
        if n > int((best or {}).get("n") or 0):
            best_h, best = int(h), stat
    return best_h, (best or {})


# ──────────────────────────────────────────────
# 读取 / 删除
# ──────────────────────────────────────────────

def list_records(creator: str | None = None, q: str = "", limit: int = 100) -> list[dict]:
    """列出回测历史（时间倒序，不含完整结果）。creator 非空仅看本人；q 模糊匹配票/信号。"""
    init()
    where, params = [], []
    if creator:
        where.append("creator = ?")
        params.append(creator)
    if q:
        where.append("(ts_code LIKE ? OR name LIKE ? OR signal_label LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    sql = f"SELECT {_LIST_COLS} FROM backtest_history"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with _conn() as con:
        rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def save_analysis(record_id: int, creator: str,
                  brief: dict | None = None, sector: dict | None = None) -> bool:
    """
    把已生成的 AI 研判 / 同类分析回填进历史记录，使点开历史可原样还原、永不重算重花钱。
    仅更新传入的字段；仅本人记录可写。返回是否更新成功。
    """
    init()
    sets, params = [], []
    if brief is not None:
        sets.append("brief=?")
        params.append(json.dumps(brief, ensure_ascii=False))
    if sector is not None:
        sets.append("sector=?")
        params.append(json.dumps(sector, ensure_ascii=False))
    if not sets:
        return False
    params += [int(record_id), creator]
    with _conn() as con:
        cur = con.execute(
            f"UPDATE backtest_history SET {','.join(sets)} WHERE id=? AND creator=?", params)
    return cur.rowcount > 0


def get_record(record_id: int, creator: str | None = None) -> dict | None:
    """取单条历史完整结果（result/brief/sector 已解析为 dict）。creator 非空时校验归属。"""
    init()
    sql = "SELECT * FROM backtest_history WHERE id = ?"
    params: list[Any] = [int(record_id)]
    if creator:
        sql += " AND creator = ?"
        params.append(creator)
    with _conn() as con:
        row = con.execute(sql, params).fetchone()
    if not row:
        return None
    rec = dict(row)
    try:
        rec["result"] = json.loads(rec["result"])
    except Exception:
        rec["result"] = {}
    for k in ("brief", "sector"):           # 可选字段：未生成或解析失败则为 None
        try:
            rec[k] = json.loads(rec[k]) if rec.get(k) else None
        except Exception:
            rec[k] = None
    return rec


def delete(record_id: int, creator: str) -> bool:
    """删除一条历史（仅创建者本人可删）。返回是否删除成功。"""
    init()
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM backtest_history WHERE id=? AND creator=?", (int(record_id), creator)
        )
    return cur.rowcount > 0
