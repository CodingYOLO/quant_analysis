"""
中枢宽表 theme_heat_all_in_one 的 SQLite 存储层（M2）。

粒度 =（theme_name × trade_date × theme_type）。前端/LLM 全部围绕这张表。
独立库 data_cache/theme_heat.db，与选股库 strategy.db 解耦。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Generator

from app.config import get_settings

logger = logging.getLogger(__name__)

_DB_FILENAME = "theme_heat.db"


@dataclass
class ThemeWideRow:
    """theme_heat_all_in_one 单行（主题 × 交易日）。None 表示数据缺失，不可填零。"""
    theme_name: str
    trade_date: str
    theme_type: str                 # industry / concept

    sample_count: int = 0
    sample_reliability: float | None = None   # %

    money_flow_1d: float | None = None        # 亿
    money_flow_3d: float | None = None
    money_flow_5d: float | None = None
    money_flow_7d: float | None = None
    money_flow_3d_norm: float | None = None   # 【需校准 C2】

    pct_chg_1d: float | None = None           # %
    pct_chg_3d: float | None = None
    pct_chg_5d: float | None = None
    pct_chg_7d: float | None = None

    breadth_ma3: float | None = None          # %
    breadth_ma5: float | None = None
    breadth_ma10: float | None = None
    breadth_ma20: float | None = None
    breadth_ma30: float | None = None
    breadth_ma60: float | None = None
    breadth_ma90: float | None = None
    breadth_ma144: float | None = None

    top100_ratio: float | None = None         # %
    top300_ratio: float | None = None

    # 人气体系（依赖东财人气榜前向积累，暂为 None=数据缺失）【需校准 C3】
    pop_weight: float | None = None
    pop_concentration_hhi: float | None = None
    pop_fairness: float | None = None

    heat_score: float | None = None           # 0-100【需校准 C1】
    heat_score_delta_3d: float | None = None
    trend: str = ""                            # new/up/down/flat
    phase: str = ""
    tier: str = ""                             # watch/buy/avoid
    nextday_risk_penalty: float | None = None

    # 成交额口径的人气集中度（已有，区别于 HHI 人气集中度）
    pop_concentration_amount: float | None = None


# ──────────────────────────────────────────────
# 连接与建表
# ──────────────────────────────────────────────

def _db_path() -> Path:
    return get_settings().cache_dir / _DB_FILENAME


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(str(_db_path()))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _columns() -> list[str]:
    return [f.name for f in fields(ThemeWideRow)]


def init_db() -> None:
    """建表（幂等）。列与 ThemeWideRow 字段一一对应。"""
    cols = _columns()
    coldefs = ",\n".join(
        f"{c} TEXT" if c in ("theme_name", "trade_date", "theme_type", "trend", "phase", "tier")
        else f"{c} REAL"
        for c in cols
    )
    with _conn() as con:
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS theme_heat_all_in_one (
                {coldefs},
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(theme_name, trade_date, theme_type)
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_theme_date ON theme_heat_all_in_one(trade_date, theme_type)"
        )


# ──────────────────────────────────────────────
# 写入 / 查询
# ──────────────────────────────────────────────

def upsert_rows(rows: list[ThemeWideRow]) -> int:
    """按 (theme_name, trade_date, theme_type) 覆盖写入，返回写入条数。"""
    if not rows:
        return 0
    init_db()
    cols = _columns()
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("theme_name", "trade_date", "theme_type"))
    sql = (
        f"INSERT INTO theme_heat_all_in_one ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(theme_name, trade_date, theme_type) DO UPDATE SET {updates}"
    )
    with _conn() as con:
        con.executemany(sql, [tuple(asdict(r)[c] for c in cols) for r in rows])
    return len(rows)


def get_themes(trade_date: str, theme_type: str | None = None) -> list[dict]:
    """查询某交易日的全部主题宽表行（按 heat_score 降序）。"""
    init_db()
    where = "WHERE trade_date=?"
    params: list = [trade_date]
    if theme_type:
        where += " AND theme_type=?"
        params.append(theme_type)
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM theme_heat_all_in_one {where} "
            f"ORDER BY (heat_score IS NULL), heat_score DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_theme(trade_date: str, theme_name: str, theme_type: str) -> dict | None:
    """查询单个主题宽表行。"""
    init_db()
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM theme_heat_all_in_one WHERE trade_date=? AND theme_name=? AND theme_type=?",
            (trade_date, theme_name, theme_type),
        ).fetchone()
    return dict(row) if row else None


def latest_trade_date(theme_type: str | None = None) -> str | None:
    """宽表中最近一个已落库的交易日。"""
    init_db()
    where = "WHERE theme_type=?" if theme_type else ""
    params = [theme_type] if theme_type else []
    with _conn() as con:
        row = con.execute(
            f"SELECT MAX(trade_date) AS d FROM theme_heat_all_in_one {where}", params
        ).fetchone()
    return row["d"] if row and row["d"] else None
