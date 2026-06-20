"""
策略验证数据库层（统一替代旧 history_tracker + tracker）。

两张核心表：
  selection_records   — 每次选股的候选股完整因子快照
  performance_records — 各时间窗口（T+1/T+3/T+5）的真实收益

设计原则：
  - 买入价统一用「次交易日开盘价」（T+1 open），不用选股当日收盘价
    （因为当日收盘无法买入，T+1 开盘才是真实可执行价格）
  - 卖出价用「持仓到期日收盘价」（T+N close）
  - is_backtest=1 表示历史回测数据，=0 表示实盘前向追踪
  - 同一 (run_date, ts_code, is_backtest) 唯一，防重复写入
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

from app.config import get_settings

logger = logging.getLogger(__name__)

_DB_FILENAME = "strategy.db"

# 支持的持仓时间窗口（交易日数）
HORIZONS = (1, 3, 5)


# ──────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────

@dataclass
class SelectionRecord:
    """候选股选股快照（写入时填充）。"""
    run_date: str           # 选股日 YYYYMMDD
    ts_code: str
    name: str
    theme: str
    market_label: str       # 强势 / 震荡 / 弱势
    is_backtest: int        # 1=回测 / 0=前向实盘

    # 量化因子（完整保存，供事后归因）
    total_score: float = 0.0
    rps50: float = 0.0
    rsi_14: float = 0.0
    vwap_deviation: float = 0.0    # %
    pullback_score: float = 0.0
    main_net_flow: float = 0.0     # 万元
    change_pct_7d: float = 0.0     # %

    # 交易价格（回测 or 前向均填入）
    entry_price: float = 0.0       # T+1 open（真实可执行买入价）
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0

    # 数据库自增ID（读取后回填）
    id: int = 0


@dataclass
class PerformanceRecord:
    """某只候选股在特定时间窗口的真实表现。"""
    selection_id: int
    horizon: int            # 1 / 3 / 5
    eval_date: str          # 卖出日 YYYYMMDD
    exit_price: float       # 卖出价（T+N close）
    pct_return: float       # 收益率 %（(exit - entry) / entry * 100）
    is_win: int             # 1=盈 / 0=亏
    hit_stop_loss: int = 0      # 期间是否触及止损价
    hit_take_profit_1: int = 0  # 期间是否触及止盈1


# ──────────────────────────────────────────────
# 数据库连接
# ──────────────────────────────────────────────

def _get_db_path() -> Path:
    settings = get_settings()
    return settings.cache_dir / _DB_FILENAME


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(str(_get_db_path()))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")   # 并发写安全
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ──────────────────────────────────────────────
# 初始化
# ──────────────────────────────────────────────

def init_db() -> None:
    """建表（幂等），可在任意时刻安全重复调用。"""
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS selection_records (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date        TEXT    NOT NULL,
                ts_code         TEXT    NOT NULL,
                name            TEXT    NOT NULL,
                theme           TEXT    NOT NULL DEFAULT '',
                market_label    TEXT    NOT NULL DEFAULT '',
                is_backtest     INTEGER NOT NULL DEFAULT 0,

                total_score     REAL    DEFAULT 0,
                rps50           REAL    DEFAULT 0,
                rsi_14          REAL    DEFAULT 0,
                vwap_deviation  REAL    DEFAULT 0,
                pullback_score  REAL    DEFAULT 0,
                main_net_flow   REAL    DEFAULT 0,
                change_pct_7d   REAL    DEFAULT 0,

                entry_price     REAL    DEFAULT 0,
                stop_loss       REAL    DEFAULT 0,
                take_profit_1   REAL    DEFAULT 0,
                take_profit_2   REAL    DEFAULT 0,

                created_at      TEXT    DEFAULT (datetime('now','localtime')),

                UNIQUE(run_date, ts_code, is_backtest)
            );

            CREATE INDEX IF NOT EXISTS idx_sel_date
                ON selection_records(run_date);
            CREATE INDEX IF NOT EXISTS idx_sel_code
                ON selection_records(ts_code);
            CREATE INDEX IF NOT EXISTS idx_sel_backtest
                ON selection_records(is_backtest);

            CREATE TABLE IF NOT EXISTS performance_records (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                selection_id    INTEGER NOT NULL
                                REFERENCES selection_records(id) ON DELETE CASCADE,
                horizon         INTEGER NOT NULL,   -- 1 / 3 / 5
                eval_date       TEXT    NOT NULL,
                exit_price      REAL    DEFAULT 0,
                pct_return      REAL    DEFAULT 0,
                is_win          INTEGER DEFAULT 0,
                hit_stop_loss   INTEGER DEFAULT 0,
                hit_take_profit_1 INTEGER DEFAULT 0,

                UNIQUE(selection_id, horizon)
            );

            CREATE INDEX IF NOT EXISTS idx_perf_sel
                ON performance_records(selection_id);
            CREATE INDEX IF NOT EXISTS idx_perf_horizon
                ON performance_records(horizon);

            CREATE TABLE IF NOT EXISTS stock_pool (
                run_date      TEXT NOT NULL,
                ts_code       TEXT NOT NULL,
                name          TEXT,
                theme         TEXT,
                theme_heat    REAL,
                sources       TEXT,    -- json[]
                strategies    TEXT,    -- json[]
                strategy_label TEXT,
                phase         TEXT,
                confidence    REAL,
                position_pct  REAL,
                buy_low       REAL,
                buy_high      REAL,
                stop_loss     REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                rps50         REAL,
                main_flow_3d  REAL,
                change_7d     REAL,
                turnover      REAL,
                vol_ratio     REAL,
                pct_chg       REAL,
                circ_mv_yi    REAL,
                close         REAL,
                is_focus      INTEGER DEFAULT 0,
                risk_flags    TEXT,    -- json[]
                reason        TEXT,    -- LLM 接地理由(S5)
                above_ma20    INTEGER, -- 均线结构
                above_ma60    INTEGER,
                slope_up      INTEGER,
                focus_score   REAL,    -- 重点分 0-100(风险调整)
                star          INTEGER DEFAULT 0,  -- 本池最强Top5
                bias20        REAL,    -- 20日乖离率(过热)
                dist_high     REAL,    -- 距120日高(高位)
                risk_penalty  REAL,    -- 风险扣分
                above_ma5     INTEGER, -- 短线均线结构
                above_ma10    INTEGER,
                ma_bull_short INTEGER,  -- MA5>MA10>MA20 短期多头排列
                winner_rate   REAL,    -- 获利盘%(抛压)
                block_discount REAL,    -- 近10日大宗量加权折溢价%(负=出货)
                created_at    TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(run_date, ts_code)
            );
            CREATE INDEX IF NOT EXISTS idx_pool_date ON stock_pool(run_date);
        """)
        # 旧库幂等补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
        existing = {row[1] for row in con.execute("PRAGMA table_info(stock_pool)")}
        for col, typ in _POOL_NEW_COLS:
            if col not in existing:
                con.execute(f"ALTER TABLE stock_pool ADD COLUMN {col} {typ}")
    logger.debug("strategy.db 初始化完成: %s", _get_db_path())


# ──────────────────────────────────────────────
# 写入选股记录
# ──────────────────────────────────────────────

def save_selections(records: list[SelectionRecord]) -> list[int]:
    """
    批量写入选股快照，返回各记录的数据库 ID。

    已存在的 (run_date, ts_code, is_backtest) 组合会被跳过（不覆盖），
    避免重复回测时数据膨胀。
    """
    if not records:
        return []

    init_db()
    ids: list[int] = []

    with _conn() as con:
        for r in records:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO selection_records (
                    run_date, ts_code, name, theme, market_label, is_backtest,
                    total_score, rps50, rsi_14, vwap_deviation,
                    pullback_score, main_net_flow, change_pct_7d,
                    entry_price, stop_loss, take_profit_1, take_profit_2
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r.run_date, r.ts_code, r.name, r.theme,
                    r.market_label, r.is_backtest,
                    r.total_score, r.rps50, r.rsi_14, r.vwap_deviation,
                    r.pullback_score, r.main_net_flow, r.change_pct_7d,
                    r.entry_price, r.stop_loss, r.take_profit_1, r.take_profit_2,
                ),
            )
            # 无论是新插入还是已存在，都拿到 rowid
            row = con.execute(
                "SELECT id FROM selection_records WHERE run_date=? AND ts_code=? AND is_backtest=?",
                (r.run_date, r.ts_code, r.is_backtest),
            ).fetchone()
            if row:
                ids.append(row["id"])

    return ids


# ──────────────────────────────────────────────
# 写入表现记录
# ──────────────────────────────────────────────

def save_performances(records: list[PerformanceRecord]) -> int:
    """
    批量写入各时间窗口表现，返回成功写入条数。
    已存在的 (selection_id, horizon) 跳过（防止重复回填）。
    """
    if not records:
        return 0

    init_db()
    count = 0
    with _conn() as con:
        for r in records:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO performance_records (
                    selection_id, horizon, eval_date, exit_price,
                    pct_return, is_win, hit_stop_loss, hit_take_profit_1
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    r.selection_id, r.horizon, r.eval_date, r.exit_price,
                    r.pct_return, r.is_win, r.hit_stop_loss, r.hit_take_profit_1,
                ),
            )
            count += cur.rowcount

    return count


# ──────────────────────────────────────────────
# 查询接口
# ──────────────────────────────────────────────

def get_pending_selections(
    is_backtest: int,
    max_horizon: int = 5,
) -> list[dict]:
    """
    查询尚未完成所有时间窗口回填的选股记录。

    Args:
        is_backtest: 1=回测 / 0=前向
        max_horizon: 最大持仓窗口（默认5）

    Returns:
        list of dict，每条包含选股记录和已有的 horizon 列表
    """
    init_db()
    with _conn() as con:
        rows = con.execute(
            """
            SELECT
                s.id, s.run_date, s.ts_code, s.name,
                s.entry_price, s.stop_loss, s.take_profit_1,
                s.market_label, s.is_backtest,
                GROUP_CONCAT(p.horizon) AS filled_horizons
            FROM selection_records s
            LEFT JOIN performance_records p ON p.selection_id = s.id
            WHERE s.is_backtest = ?
              AND s.entry_price > 0
            GROUP BY s.id
            HAVING (
                SELECT COUNT(*) FROM performance_records
                WHERE selection_id = s.id
            ) < ?
            ORDER BY s.run_date ASC
            """,
            (is_backtest, len(HORIZONS)),
        ).fetchall()

    result = []
    for row in rows:
        filled = set()
        if row["filled_horizons"]:
            filled = {int(h) for h in row["filled_horizons"].split(",")}
        result.append({
            **dict(row),
            "filled_horizons": filled,
            "pending_horizons": [h for h in HORIZONS if h not in filled],
        })
    return result


def get_all_with_performance(
    is_backtest: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """
    联合查询选股记录 + 所有时间窗口表现（用于分析）。

    Returns:
        每条记录包含 selection 所有字段 + T1/T3/T5 的 pct_return 和 is_win
    """
    init_db()
    conditions = []
    params: list = []

    if is_backtest is not None:
        conditions.append("s.is_backtest = ?")
        params.append(is_backtest)
    if start_date:
        conditions.append("s.run_date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("s.run_date <= ?")
        params.append(end_date)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with _conn() as con:
        rows = con.execute(
            f"""
            SELECT
                s.*,
                p1.pct_return  AS t1_return,
                p1.is_win      AS t1_win,
                p1.hit_stop_loss AS t1_stop,
                p3.pct_return  AS t3_return,
                p3.is_win      AS t3_win,
                p5.pct_return  AS t5_return,
                p5.is_win      AS t5_win
            FROM selection_records s
            LEFT JOIN performance_records p1
                ON p1.selection_id = s.id AND p1.horizon = 1
            LEFT JOIN performance_records p3
                ON p3.selection_id = s.id AND p3.horizon = 3
            LEFT JOIN performance_records p5
                ON p5.selection_id = s.id AND p5.horizon = 5
            {where}
            ORDER BY s.run_date DESC, s.total_score DESC
            """,
            params,
        ).fetchall()

    return [dict(r) for r in rows]


_POOL_COLS = [
    "run_date", "ts_code", "name", "theme", "theme_heat", "sources", "strategies",
    "strategy_label", "phase", "confidence", "position_pct", "buy_low", "buy_high",
    "stop_loss", "take_profit_1", "take_profit_2", "rps50", "main_flow_3d", "change_7d",
    "turnover", "vol_ratio", "pct_chg", "circ_mv_yi", "close", "is_focus", "risk_flags", "reason",
    # 2026-06-20 新增：均线结构 + 重点分(风险调整) + 星标 + 风险/位置(乖离/距高点/风险扣分)
    "above_ma20", "above_ma60", "slope_up", "focus_score", "star",
    "bias20", "dist_high", "risk_penalty",
    "above_ma5", "above_ma10", "ma_bull_short",
    "winner_rate", "block_discount",
]

# 旧库兼容：新增列（init_db 幂等补列，避免改 CREATE TABLE 后旧库缺列）
_POOL_NEW_COLS = [
    ("above_ma20", "INTEGER"), ("above_ma60", "INTEGER"), ("slope_up", "INTEGER"),
    ("focus_score", "REAL"), ("star", "INTEGER DEFAULT 0"),
    ("bias20", "REAL"), ("dist_high", "REAL"), ("risk_penalty", "REAL"),
    ("above_ma5", "INTEGER"), ("above_ma10", "INTEGER"), ("ma_bull_short", "INTEGER"),
    ("winner_rate", "REAL"), ("block_discount", "REAL"),
]
_POOL_JSON = {"sources", "strategies", "risk_flags"}


def save_pool(run_date: str, records: list[dict]) -> int:
    """覆盖写入某交易日选股池。records 为 stock_pool 引擎输出的 dict 列表。"""
    if not records:
        return 0
    init_db()
    ph = ",".join("?" for _ in _POOL_COLS)
    upd = ",".join(f"{c}=excluded.{c}" for c in _POOL_COLS if c not in ("run_date", "ts_code"))
    with _conn() as con:
        # 真·覆盖写入：先清当日旧池，避免改选/换行业口径后残留陈股（否则旧选股会以旧数据滞留）
        con.execute("DELETE FROM stock_pool WHERE run_date=?", (run_date,))
        for r in records:
            vals = []
            for c in _POOL_COLS:
                v = r.get(c)
                if c == "run_date":
                    v = run_date
                elif c == "is_focus":
                    v = 1 if r.get("is_focus") else 0
                elif c in _POOL_JSON:
                    v = json.dumps(r.get(c, []), ensure_ascii=False)
                vals.append(v)
            con.execute(
                f"INSERT INTO stock_pool ({','.join(_POOL_COLS)}) VALUES ({ph}) "
                f"ON CONFLICT(run_date, ts_code) DO UPDATE SET {upd}",
                vals,
            )
    return len(records)


def get_pool_with_perf(run_date: str) -> list[dict]:
    """读取某交易日选股池，左联 T+1/T+3/T+5 收益（来自前向追踪 performance）。"""
    init_db()
    with _conn() as con:
        rows = con.execute(
            """
            SELECT sp.*,
                   p1.pct_return AS t1_return, p3.pct_return AS t3_return, p5.pct_return AS t5_return
            FROM stock_pool sp
            LEFT JOIN selection_records s
                ON s.run_date=sp.run_date AND s.ts_code=sp.ts_code AND s.is_backtest=0
            LEFT JOIN performance_records p1 ON p1.selection_id=s.id AND p1.horizon=1
            LEFT JOIN performance_records p3 ON p3.selection_id=s.id AND p3.horizon=3
            LEFT JOIN performance_records p5 ON p5.selection_id=s.id AND p5.horizon=5
            WHERE sp.run_date=?
            ORDER BY sp.is_focus DESC, sp.confidence DESC
            """,
            (run_date,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in _POOL_JSON:
            try:
                d[k] = json.loads(d[k]) if d.get(k) else []
            except Exception:
                d[k] = []
        out.append(d)
    return out


def theme_win_rates(min_samples: int = 1) -> dict[str, dict]:
    """
    按主题(行业)计算历史 T+1 胜率（现行库口径，替代已废弃的 history_tracker）。

    数据源：selection_records.theme JOIN performance_records(horizon=1)。
    含回测与实盘样本以扩大统计量（pct_return>0 计为胜）。

    Args:
        min_samples: 最少样本数才纳入。
    Returns:
        {theme: {"win_rate": 0.0~1.0, "samples": int, "avg_return": float}}
    """
    init_db()
    with _conn() as con:
        rows = con.execute(
            """
            SELECT s.theme AS theme,
                   COUNT(*) AS samples,
                   AVG(CASE WHEN p.pct_return > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                   AVG(p.pct_return) AS avg_return
            FROM selection_records s
            JOIN performance_records p
              ON p.selection_id = s.id AND p.horizon = 1
            WHERE p.pct_return IS NOT NULL AND s.theme IS NOT NULL AND s.theme <> ''
            GROUP BY s.theme
            HAVING COUNT(*) >= ?
            """,
            (min_samples,),
        ).fetchall()
    return {
        r["theme"]: {
            "win_rate": round(r["win_rate"], 3),
            "samples": int(r["samples"]),
            "avg_return": round(r["avg_return"] or 0.0, 2),
        }
        for r in rows
    }


def pool_dates() -> list[str]:
    """已落库的选股池交易日（降序）。"""
    init_db()
    with _conn() as con:
        rows = con.execute("SELECT DISTINCT run_date FROM stock_pool ORDER BY run_date DESC").fetchall()
    return [r["run_date"] for r in rows]


def pool_gen_time(run_date: str) -> str | None:
    """
    某交易日选股池的生成时间（取该日最早一条 created_at，即首次落库时间）。

    Args:
        run_date: 选股日 YYYYMMDD
    Returns:
        'YYYY-MM-DD HH:MM:SS' 字符串（服务器本地时区=北京时间），无记录时 None。
    """
    init_db()
    with _conn() as con:
        row = con.execute(
            "SELECT MIN(created_at) AS gen FROM stock_pool WHERE run_date=?",
            (run_date,),
        ).fetchone()
    return row["gen"] if row and row["gen"] else None


def get_summary_stats(is_backtest: int | None = None) -> dict:
    """快速查询各时间窗口的汇总统计（胜率、均值等）。"""
    init_db()
    conditions = ["p.pct_return IS NOT NULL"]
    params: list = []
    if is_backtest is not None:
        conditions.append("s.is_backtest = ?")
        params.append(is_backtest)
    where = "WHERE " + " AND ".join(conditions)

    with _conn() as con:
        stats = {}
        for h in HORIZONS:
            row = con.execute(
                f"""
                SELECT
                    COUNT(*)            AS total,
                    SUM(p.is_win)       AS wins,
                    AVG(p.pct_return)   AS avg_return,
                    MIN(p.pct_return)   AS min_return,
                    MAX(p.pct_return)   AS max_return,
                    SUM(p.hit_stop_loss) AS stop_count
                FROM performance_records p
                JOIN selection_records s ON s.id = p.selection_id
                {where}
                  AND p.horizon = ?
                """,
                params + [h],
            ).fetchone()
            total = row["total"] or 0
            wins = row["wins"] or 0
            stats[f"t{h}"] = {
                "total": total,
                "win_rate": round(wins / total, 4) if total > 0 else None,
                "avg_return": round(row["avg_return"] or 0, 4),
                "min_return": round(row["min_return"] or 0, 4),
                "max_return": round(row["max_return"] or 0, 4),
                "stop_rate": round((row["stop_count"] or 0) / total, 4) if total > 0 else None,
            }
    return stats
