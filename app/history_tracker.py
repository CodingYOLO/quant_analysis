"""
历史胜率追踪器（O13）。

每次pipeline运行结束后：
1. 将当日候选股记录到 SQLite
2. 次日运行时，回填昨日候选股的实际涨跌幅
3. 按主题聚合计算历史胜率（T+1 次日涨跌 > 0 为胜）

数据库位置：data_cache/history.db
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

_DB_FILENAME = "history.db"


# ──────────────────────────────────────────────
# 数据库初始化
# ──────────────────────────────────────────────

def _get_db_path() -> Path:
    settings = get_settings()
    return settings.cache_dir / _DB_FILENAME


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """创建数据库表（幂等）。"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS candidate_records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date  TEXT NOT NULL,      -- 选出日 YYYYMMDD
                ts_code     TEXT NOT NULL,
                name        TEXT NOT NULL,
                theme       TEXT NOT NULL,
                close_price REAL,               -- 选出日收盘价
                -- 次日回填字段
                next_date   TEXT,               -- 次交易日 YYYYMMDD
                next_close  REAL,               -- 次日收盘价
                pct_change  REAL,               -- 次日涨跌幅（%）
                is_win      INTEGER,            -- 1=涨 / 0=跌 / NULL=未回填
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_trade_date
                ON candidate_records(trade_date);

            CREATE INDEX IF NOT EXISTS idx_theme
                ON candidate_records(theme);
        """)
    logger.debug("历史数据库初始化完成: %s", _get_db_path())


# ──────────────────────────────────────────────
# 写入候选股
# ──────────────────────────────────────────────

def save_candidates(
    trade_date: str,
    candidates: list[dict],
) -> None:
    """
    将今日通过多空辩论的候选股写入数据库。

    Args:
        trade_date: 交易日 YYYYMMDD
        candidates: list of dict，每个 dict 包含 ts_code/name/theme/close_price
    """
    if not candidates:
        return

    init_db()
    rows = [
        (
            trade_date,
            c["ts_code"],
            c["name"],
            c.get("theme", ""),
            c.get("close_price", None),
        )
        for c in candidates
    ]
    with _get_conn() as conn:
        # 同一天同一只股票只写一次
        conn.executemany(
            """
            INSERT OR IGNORE INTO candidate_records
                (trade_date, ts_code, name, theme, close_price)
            SELECT ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM candidate_records
                WHERE trade_date=? AND ts_code=?
            )
            """,
            [(r[0], r[1], r[2], r[3], r[4], r[0], r[1]) for r in rows],
        )
    logger.info("[历史追踪] 写入 %d 条候选股记录，交易日=%s", len(rows), trade_date)


# ──────────────────────────────────────────────
# 回填次日涨跌幅
# ──────────────────────────────────────────────

def backfill_results(
    next_date: str,
    price_map: dict[str, float],
) -> int:
    """
    用次日收盘价回填上一个交易日的候选股表现。

    Args:
        next_date:  次交易日 YYYYMMDD
        price_map:  {ts_code: close_price}

    Returns:
        回填成功的条数
    """
    if not price_map:
        return 0

    init_db()
    updated = 0
    with _get_conn() as conn:
        # 找出尚未回填且 next_date 为空的最近记录
        rows = conn.execute(
            "SELECT id, ts_code, close_price FROM candidate_records WHERE next_date IS NULL"
        ).fetchall()

        for row in rows:
            next_close = price_map.get(row["ts_code"])
            if next_close is None or row["close_price"] is None:
                continue
            pct = round((next_close / row["close_price"] - 1) * 100, 2)
            is_win = 1 if pct > 0 else 0
            conn.execute(
                """
                UPDATE candidate_records
                SET next_date=?, next_close=?, pct_change=?, is_win=?
                WHERE id=?
                """,
                (next_date, next_close, pct, is_win, row["id"]),
            )
            updated += 1

    logger.info("[历史追踪] 回填 %d 条次日表现，next_date=%s", updated, next_date)
    return updated


# ──────────────────────────────────────────────
# 查询历史胜率
# ──────────────────────────────────────────────

def get_theme_win_rates(min_samples: int = 3) -> dict[str, dict]:
    """
    按主题计算历史胜率（T+1胜率）。

    Args:
        min_samples: 最少样本数才纳入统计

    Returns:
        {theme_name: {"win_rate": 0.60, "samples": 10, "avg_pct": 1.2}}
    """
    init_db()
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                theme,
                COUNT(*) AS samples,
                SUM(is_win) AS wins,
                AVG(pct_change) AS avg_pct
            FROM candidate_records
            WHERE is_win IS NOT NULL
            GROUP BY theme
            HAVING COUNT(*) >= ?
            """,
            (min_samples,),
        ).fetchall()

    result: dict[str, dict] = {}
    for row in rows:
        theme = row["theme"]
        result[theme] = {
            "win_rate": round(row["wins"] / row["samples"], 2),
            "samples": row["samples"],
            "avg_pct": round(row["avg_pct"] or 0, 2),
        }
    return result


def get_stock_history(ts_code: str) -> list[dict]:
    """查询某只股票的完整历史记录。"""
    init_db()
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT trade_date, name, theme, close_price, next_date, pct_change, is_win
            FROM candidate_records
            WHERE ts_code=?
            ORDER BY trade_date DESC
            """,
            (ts_code,),
        ).fetchall()
    return [dict(r) for r in rows]
