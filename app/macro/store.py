"""宏观面板数据库层（SQLite·`data_cache/macro.db`）。

为什么独立于 `strategy.db`：宏观数据可独立回补/清空/重建，不牵连策略库；
且完全不必改动 `app/strategy/db.py` 一行（守住"不重构现有代码"）。

**WAL 是必需的，不是可选**：写入方是 cron（`macro-sync`），读取方是常驻 FastAPI 进程，
两者并发。WAL 下读不阻塞写、写不阻塞读；非 WAL 会在盘后写入时把 Web 请求锁住。

跨库联查（只读）见 `docs/宏观指标口径说明_*.md`：
    ATTACH DATABASE '<...>/strategy.db' AS s;
**注意**：WAL 模式下 SQLite 不支持跨库写事务，故本模块只写 macro.db，绝不跨库写。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterable, Sequence

from app.config import get_settings

logger = logging.getLogger(__name__)

_DB_FILENAME = "macro.db"

# 分层顺序（决定前端从上到下的展示次序，也是传导链条的上游→下游次序）
LAYERS: tuple[str, ...] = ("L0_liquidity", "L1_flow", "L2_sentiment", "L3_external")

LAYER_LABELS: dict[str, str] = {
    "L0_liquidity": "L0 流动性",
    "L1_flow": "L1 市场内部资金",
    "L2_sentiment": "L2 情绪温度",
    "L3_external": "L3 外部输入",
}


def db_path() -> Path:
    """宏观库路径。与项目其它缓存同放 `data_cache/`，便于统一备份。"""
    return get_settings().cache_dir / _DB_FILENAME


def strategy_db_path() -> Path:
    """策略库路径（仅供文档/回测脚本做 ATTACH 只读联查用，本模块不写它）。"""
    return get_settings().cache_dir / "strategy.db"


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    """连接宏观库。WAL + 外键，与 `app/strategy/db.py` 保持同一套连接约定。"""
    con = sqlite3.connect(str(db_path()))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")     # cron写 + web读 并发必需
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")    # 写入期间 Web 读请求最多等 5s 而非立刻报错
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


_SCHEMA = """
-- 指标元数据：**配置驱动的唯一真源**。前端与计算层不得硬编码任何指标列表。
CREATE TABLE IF NOT EXISTS metric_meta (
    code        TEXT PRIMARY KEY,
    name_cn     TEXT    NOT NULL,
    layer       TEXT    NOT NULL,          -- L0_liquidity|L1_flow|L2_sentiment|L3_external
    freq        TEXT    NOT NULL,          -- daily|weekly|monthly
    unit        TEXT    NOT NULL DEFAULT '',
    direction   INTEGER NOT NULL DEFAULT 0,-- 1=数值高对A股偏多 -1=偏空 0=中性(不参与评分)
    weight      REAL    NOT NULL DEFAULT 1.0,
    source      TEXT    NOT NULL DEFAULT '',   -- 主源：eastmoney|sina|tushare|akshare|internal|derived
    source_fallback TEXT NOT NULL DEFAULT '',  -- 备源(主源失败时降级)，为空=无备源，失败即写 NULL
    api         TEXT    NOT NULL DEFAULT '',   -- 具体接口名/secid，排查用
    lag_days    INTEGER NOT NULL DEFAULT 0,    -- 数据发布相对交易日的滞后
    enabled     INTEGER NOT NULL DEFAULT 1,
    hist_break  TEXT    NOT NULL DEFAULT '',   -- 断点日，逗号分隔 YYYYMMDD（可多个）
    break_mode  TEXT    NOT NULL DEFAULT 'truncate',  -- truncate=硬截断窗口 | mark=只标注不截断
    score_from  TEXT    NOT NULL DEFAULT '',   -- 该日起才参与分层评分；之前只展示不计分（见下）
    sort_order  INTEGER NOT NULL DEFAULT 100,
    note        TEXT    NOT NULL DEFAULT '',
    updated_at  TEXT    DEFAULT (datetime('now','localtime'))
);
-- score_from 的必要性：break_mode='mark' 只画竖线，那是**给人看的**；
-- 而 layer_score 是机器算的，它看不见竖线。制度断点（如杠杆上限 1.25→1.00）会把指标中枢
-- 系统性下移，评分函数会把"制度性下移"误读成"杠杆情绪降温"，导致该层得分虚高数月。
-- 故断点后一段时间（默认取断点+3个月）内该指标 score_from 未到 → 权重按 0 处理，只展示不计分。
CREATE INDEX IF NOT EXISTS idx_meta_layer ON metric_meta(layer, sort_order);

-- 指标日频长表。value 为 NULL = 该日未取到（禁止用 0 或昨值填充）
CREATE TABLE IF NOT EXISTS macro_daily (
    trade_date    TEXT NOT NULL,           -- 交易日 YYYYMMDD
    code          TEXT NOT NULL,
    value         REAL,
    chg_1d        REAL,
    chg_5d        REAL,
    zscore_250    REAL,
    pctile_750    REAL,                    -- 0-100·point-in-time
    sample_n      INTEGER,                 -- 实际参与分位计算的样本数（断点后会少于窗口长度）
    as_of         TEXT,                    -- 该数值对应的真实数据时点 YYYYMMDD
    source        TEXT,                    -- **实际取到该值的源**（主源还是备源），事后排查用
    is_stale      INTEGER NOT NULL DEFAULT 0,  -- 1=forward fill 而来（月频/周频）
    ingested_at   TEXT DEFAULT (datetime('now','localtime')),
    source_run_id TEXT,
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_daily_code_date ON macro_daily(code, trade_date);

-- 取数运行日志：验收要求"看到每个指标的成功/失败/耗时"
CREATE TABLE IF NOT EXISTS macro_run_log (
    run_id      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    code        TEXT NOT NULL,
    status      TEXT NOT NULL,             -- ok|empty|stale|partial|error
    rows        INTEGER NOT NULL DEFAULT 0,
    elapsed_ms  INTEGER NOT NULL DEFAULT 0,
    err_msg     TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (run_id, code)
);
CREATE INDEX IF NOT EXISTS idx_runlog_date ON macro_run_log(trade_date);

-- 事件日历（支持手工录入补充）
CREATE TABLE IF NOT EXISTS macro_calendar (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date  TEXT    NOT NULL,          -- YYYYMMDD
    event_type  TEXT    NOT NULL,          -- fomc|us_cpi|us_nfp|cn_data|lpr|meeting|earnings|float_release
    title       TEXT    NOT NULL,
    importance  INTEGER NOT NULL DEFAULT 2,-- 1低 2中 3高
    region      TEXT    NOT NULL DEFAULT '',
    expected    TEXT    NOT NULL DEFAULT '',
    actual      TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    is_manual   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(event_date, event_type, title)
);
CREATE INDEX IF NOT EXISTS idx_cal_date ON macro_calendar(event_date);

-- 每日 LLM 摘要存档（可按日期回读，供回看模式）
CREATE TABLE IF NOT EXISTS macro_summary (
    trade_date  TEXT PRIMARY KEY,
    liquidity   TEXT NOT NULL DEFAULT '',
    flow        TEXT NOT NULL DEFAULT '',
    external    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""

# 旧库幂等补列：(表名, 列名, 类型与默认值)
_NEW_COLS: tuple[tuple[str, str, str], ...] = (
    ("macro_daily", "sample_n", "INTEGER"),
    ("macro_daily", "source", "TEXT"),
    ("macro_daily", "is_stale", "INTEGER NOT NULL DEFAULT 0"),
    ("metric_meta", "break_mode", "TEXT NOT NULL DEFAULT 'truncate'"),
    ("metric_meta", "score_from", "TEXT NOT NULL DEFAULT ''"),
    ("metric_meta", "source_fallback", "TEXT NOT NULL DEFAULT ''"),
    ("metric_meta", "sort_order", "INTEGER NOT NULL DEFAULT 100"),
)


def init_db() -> None:
    """建表（幂等），可在任意时刻安全重复调用。"""
    with _conn() as con:
        con.executescript(_SCHEMA)
        for table, col, decl in _NEW_COLS:
            existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                logger.info("[macro] 补列 %s.%s", table, col)
    logger.debug("[macro] macro.db 初始化完成: %s", db_path())


# ──────────────────────────────────────────────
# metric_meta
# ──────────────────────────────────────────────

_META_COLS = ("code", "name_cn", "layer", "freq", "unit", "direction", "weight",
              "source", "source_fallback", "api", "lag_days", "enabled", "hist_break",
              "break_mode", "score_from", "sort_order", "note")


def upsert_meta(rows: Iterable[dict]) -> int:
    """写入/更新指标元数据。

    **只覆盖定义字段**：`weight` / `enabled` / `hist_break` / `break_mode` 是用户可调项，
    代码里改了注册表不应该把用户在库里的手工调整冲掉——故这四列用 COALESCE 保留库内已有值。
    要强制重置用 `reset_meta_tuning()`。
    """
    tunable = {"weight", "enabled", "hist_break", "break_mode", "score_from"}
    sets = ", ".join(
        f"{c}=COALESCE((SELECT {c} FROM metric_meta WHERE code=excluded.code), excluded.{c})"
        if c in tunable else f"{c}=excluded.{c}"
        for c in _META_COLS if c != "code"
    )
    sql = (f"INSERT INTO metric_meta ({','.join(_META_COLS)}) "
           f"VALUES ({','.join('?' * len(_META_COLS))}) "
           f"ON CONFLICT(code) DO UPDATE SET {sets}, updated_at=datetime('now','localtime')")
    payload = [tuple(r.get(c) for c in _META_COLS) for r in rows]
    with _conn() as con:
        con.executemany(sql, payload)
    return len(payload)


def reset_meta_tuning(codes: Sequence[str] | None = None) -> int:
    """把可调项（weight/enabled/hist_break/break_mode）强制重置回注册表默认值。"""
    from app.macro.registry import METRICS
    defs = {m.code: m for m in METRICS if codes is None or m.code in codes}
    with _conn() as con:
        for code, m in defs.items():
            con.execute("UPDATE metric_meta SET weight=?, enabled=?, hist_break=?, break_mode=?, "
                        "score_from=? WHERE code=?",
                        (m.weight, int(m.enabled), m.hist_break, m.break_mode, m.score_from, code))
    return len(defs)


def get_meta(layer: str | None = None, enabled_only: bool = True) -> list[dict]:
    """读指标元数据（前端与计算层的唯一指标来源）。"""
    sql = "SELECT * FROM metric_meta WHERE 1=1"
    args: list[Any] = []
    if enabled_only:
        sql += " AND enabled=1"
    if layer:
        sql += " AND layer=?"
        args.append(layer)
    sql += " ORDER BY layer, sort_order, code"
    with _conn() as con:
        return [dict(r) for r in con.execute(sql, args)]


# ──────────────────────────────────────────────
# macro_daily
# ──────────────────────────────────────────────

_DAILY_COLS = ("trade_date", "code", "value", "chg_1d", "chg_5d", "zscore_250",
               "pctile_750", "sample_n", "as_of", "source", "is_stale", "source_run_id")


# 非空列的兜底值：显式传 None 会绕过 SQL DEFAULT 触发 NOT NULL 报错，故在此归一
_DAILY_DEFAULTS: dict[str, Any] = {"is_stale": 0, "source_run_id": ""}


def upsert_daily(rows: Iterable[dict]) -> int:
    """写入指标日值（按 (trade_date, code) 覆盖）。

    `value=None` **原样写 NULL**——这是取数失败的正确表达，绝不能被兜底成 0
    （0 会被下游当成真实值参与分位与异动判定）。只有非空约束列才走 `_DAILY_DEFAULTS`。
    """
    sets = ", ".join(f"{c}=excluded.{c}" for c in _DAILY_COLS if c not in ("trade_date", "code"))
    sql = (f"INSERT INTO macro_daily ({','.join(_DAILY_COLS)}) "
           f"VALUES ({','.join('?' * len(_DAILY_COLS))}) "
           f"ON CONFLICT(trade_date, code) DO UPDATE SET {sets}, "
           f"ingested_at=datetime('now','localtime')")
    payload = []
    for r in rows:
        vals = []
        for c in _DAILY_COLS:
            v = r.get(c)
            vals.append(_DAILY_DEFAULTS[c] if v is None and c in _DAILY_DEFAULTS else v)
        payload.append(tuple(vals))
    with _conn() as con:
        con.executemany(sql, payload)
    return len(payload)


def read_series(code: str, end_date: str, limit: int | None = None) -> list[dict]:
    """读单指标到 end_date 为止的序列（升序）。

    **回看模式的 point-in-time 保证在此**：一律 `trade_date <= end_date`，
    绝不返回该日之后的任何数据。
    """
    sql = ("SELECT trade_date, value, as_of, source, is_stale FROM macro_daily "
           "WHERE code=? AND trade_date<=? ORDER BY trade_date")
    with _conn() as con:
        rows = [dict(r) for r in con.execute(sql, (code, end_date))]
    return rows[-limit:] if limit else rows


def read_panel(trade_date: str) -> dict[str, dict]:
    """读某日全部指标行 → {code: row}。"""
    with _conn() as con:
        return {r["code"]: dict(r) for r in
                con.execute("SELECT * FROM macro_daily WHERE trade_date=?", (trade_date,))}


def latest_date() -> str:
    """库中最新有数据的交易日；空库返回空串。"""
    with _conn() as con:
        row = con.execute("SELECT MAX(trade_date) AS d FROM macro_daily").fetchone()
    return (row["d"] or "") if row else ""


def available_dates(limit: int = 60) -> list[str]:
    """库中最近 N 个有数据的交易日（降序）——供回看模式的日期选择器。"""
    with _conn() as con:
        return [r["trade_date"] for r in con.execute(
            "SELECT DISTINCT trade_date FROM macro_daily ORDER BY trade_date DESC LIMIT ?", (limit,))]


# ──────────────────────────────────────────────
# 运行日志 / 日历 / 摘要
# ──────────────────────────────────────────────

def log_runs(rows: Iterable[dict]) -> int:
    sql = ("INSERT INTO macro_run_log (run_id, trade_date, code, status, rows, elapsed_ms, err_msg) "
           "VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id, code) DO UPDATE SET "
           "status=excluded.status, rows=excluded.rows, elapsed_ms=excluded.elapsed_ms, "
           "err_msg=excluded.err_msg")
    payload = [(r["run_id"], r["trade_date"], r["code"], r["status"],
                int(r.get("rows", 0)), int(r.get("elapsed_ms", 0)), str(r.get("err_msg", "")))
               for r in rows]
    with _conn() as con:
        con.executemany(sql, payload)
    return len(payload)


def read_run_log(trade_date: str) -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM macro_run_log WHERE trade_date=? ORDER BY status, code", (trade_date,))]


def upsert_calendar(rows: Iterable[dict]) -> int:
    sql = ("INSERT INTO macro_calendar (event_date, event_type, title, importance, region, "
           "expected, actual, note, is_manual) VALUES (?,?,?,?,?,?,?,?,?) "
           "ON CONFLICT(event_date, event_type, title) DO UPDATE SET "
           "importance=excluded.importance, region=excluded.region, "
           "expected=excluded.expected, actual=excluded.actual, note=excluded.note")
    payload = [(r["event_date"], r["event_type"], r["title"], int(r.get("importance", 2)),
                r.get("region", ""), r.get("expected", ""), r.get("actual", ""),
                r.get("note", ""), int(r.get("is_manual", 0))) for r in rows]
    with _conn() as con:
        con.executemany(sql, payload)
    return len(payload)


def read_calendar(start_date: str, end_date: str) -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM macro_calendar WHERE event_date BETWEEN ? AND ? "
            "ORDER BY event_date, importance DESC", (start_date, end_date))]


def upsert_summary(trade_date: str, liquidity: str, flow: str, external: str, model: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO macro_summary (trade_date, liquidity, flow, external, model) "
            "VALUES (?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
            "liquidity=excluded.liquidity, flow=excluded.flow, external=excluded.external, "
            "model=excluded.model, created_at=datetime('now','localtime')",
            (trade_date, liquidity, flow, external, model))


def read_summary(trade_date: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM macro_summary WHERE trade_date=?", (trade_date,)).fetchone()
    return dict(row) if row else None
