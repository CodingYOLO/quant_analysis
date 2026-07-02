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
            CREATE TABLE IF NOT EXISTS pool_eval (
                run_date    TEXT NOT NULL,
                source      TEXT NOT NULL,    -- backtest(历史价格结构) / forward(真实池完整评分)
                tier        TEXT NOT NULL,    -- 强/中/弱 或 ⭐重点/高分/其余
                n           INTEGER,
                win_rate    REAL,             -- T+5 胜率%
                avg_return  REAL,             -- T+5 均收益%
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(run_date, source, tier)
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                owner       TEXT NOT NULL DEFAULT 'me',  -- 归属：me=用户1(我) / dad=用户2(爸爸)，多人各自一份
                ts_code     TEXT NOT NULL,      -- 完整代码 600519.SH
                name        TEXT,
                is_holding  INTEGER DEFAULT 0,  -- 0=仅自选盯盘 / 1=持仓
                cost        REAL,               -- 持仓成本价（可空）
                shares      REAL,               -- 持仓数量/股（可空）
                stop_loss   REAL,               -- 止损价（可空·用于跌破止损预警）
                target_price REAL,              -- 目标买入价（可空·用于逼近买入区提醒）
                note        TEXT,               -- 备注（如买入逻辑）
                added_at    TEXT DEFAULT (datetime('now','localtime')),
                updated_at  TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (owner, ts_code)    -- 同一只票可同时在不同人的列表里
            );
            CREATE TABLE IF NOT EXISTS trade_plan (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code     TEXT NOT NULL,        -- 600519.SH
                name        TEXT,
                side        TEXT DEFAULT 'buy',   -- buy/sell
                buy_price   REAL,                 -- 计划买入价(限价上限·QMT不追高于此)
                stop_loss   REAL,                 -- 止损价
                take_profit REAL,                 -- 止盈价(可空)
                position_pct REAL,                -- 仓位(0.1=10%)
                note        TEXT,                 -- 我的决策理由
                status      TEXT DEFAULT 'pending',-- pending/done/cancelled
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT,
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                updated_at  TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL,
                role        TEXT NOT NULL,       -- user / assistant
                content     TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_chatmsg_sid ON chat_messages(session_id);
            CREATE TABLE IF NOT EXISTS perception_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code       TEXT, name TEXT, t0 TEXT,    -- 题目(揭晓后才记)
                setup_tag     TEXT, market_state TEXT,     -- 形态/大盘状态(分组统计用)
                pred          TEXT, actual TEXT,           -- 预测档 / 实际档
                ret_fwd       REAL,                        -- 实际持有N日收益%
                points        REAL, direction_right INTEGER,
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            );
            -- 人气榜每日快照(自建轨迹·算峰值/谷值/回升)。kind: rank人气/up飙升
            CREATE TABLE IF NOT EXISTS hot_rank_log (
                trade_date  TEXT NOT NULL,
                kind        TEXT NOT NULL DEFAULT 'rank',
                code        TEXT NOT NULL,      -- 6位代码
                name        TEXT,
                rank        INTEGER,            -- 当日名次(越小越火)
                rank_chg    INTEGER,            -- 较昨日名次变动
                PRIMARY KEY (trade_date, kind, code)
            );
            -- 认知脚手架：每日「5问框架」推演日志(练框架·事后校准命中率)。每交易日一条
            CREATE TABLE IF NOT EXISTS cognition_log (
                trade_date  TEXT PRIMARY KEY,
                q1_regime   TEXT, q2_mainline TEXT, q3_tempo TEXT,   -- 定性/主线/节奏
                q4_catalyst TEXT, q5_path     TEXT,                  -- 催化/风险路径
                stance      TEXT,       -- 整体立场：进攻/均衡/防守/空仓
                main_line   TEXT,       -- 主押主线(板块名)
                confidence  INTEGER,    -- 信心 1-5
                review_note TEXT,       -- 事后自评(回看时填)
                sh_close    REAL,       -- 记录当日上证收盘(供客观校准·后填)
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                updated_at  TEXT DEFAULT (datetime('now','localtime'))
            );
        """)
        # 旧库幂等补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
        existing = {row[1] for row in con.execute("PRAGMA table_info(stock_pool)")}
        for col, typ in _POOL_NEW_COLS:
            if col not in existing:
                con.execute(f"ALTER TABLE stock_pool ADD COLUMN {col} {typ}")
        wl_cols = {row[1] for row in con.execute("PRAGMA table_info(watchlist)")}
        if "target_price" not in wl_cols:
            con.execute("ALTER TABLE watchlist ADD COLUMN target_price REAL")
        if "owner" not in wl_cols:
            _migrate_watchlist_add_owner(con)       # 单列PK(ts_code) → 复合PK(owner,ts_code)·老数据归 'me'
    logger.debug("strategy.db 初始化完成: %s", _get_db_path())


def _migrate_watchlist_add_owner(con) -> None:
    """旧库升级：watchlist 加 owner 列并改复合主键 (owner, ts_code)。原有自选/持仓全部归 'me'(用户1)。

    SQLite 不支持直接改主键，须整表重建。重建在同一事务内完成，失败回滚不丢数据。
    """
    con.executescript(
        """
        ALTER TABLE watchlist RENAME TO _watchlist_old;
        CREATE TABLE watchlist (
            owner TEXT NOT NULL DEFAULT 'me', ts_code TEXT NOT NULL, name TEXT,
            is_holding INTEGER DEFAULT 0, cost REAL, shares REAL, stop_loss REAL,
            target_price REAL, note TEXT,
            added_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (owner, ts_code)
        );
        INSERT INTO watchlist (owner, ts_code, name, is_holding, cost, shares,
                               stop_loss, target_price, note, added_at, updated_at)
            SELECT 'me', ts_code, name, is_holding, cost, shares,
                   stop_loss, target_price, note, added_at, updated_at FROM _watchlist_old;
        DROP TABLE _watchlist_old;
        """
    )
    logger.info("[db] watchlist 已升级为多归属(owner,ts_code)·原数据归 'me'")


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
    # 2026-06-26 新增：龙虎榜机构真钱（净买/天数/重点分真钱加分）
    "inst_net_yi", "inst_buy_days", "inst_bonus",
]

# 旧库兼容：新增列（init_db 幂等补列，避免改 CREATE TABLE 后旧库缺列）
_POOL_NEW_COLS = [
    ("above_ma20", "INTEGER"), ("above_ma60", "INTEGER"), ("slope_up", "INTEGER"),
    ("focus_score", "REAL"), ("star", "INTEGER DEFAULT 0"),
    ("bias20", "REAL"), ("dist_high", "REAL"), ("risk_penalty", "REAL"),
    ("above_ma5", "INTEGER"), ("above_ma10", "INTEGER"), ("ma_bull_short", "INTEGER"),
    ("winner_rate", "REAL"), ("block_discount", "REAL"),
    ("inst_net_yi", "REAL"), ("inst_buy_days", "INTEGER"), ("inst_bonus", "REAL"),
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


def save_evals(evals: list[dict]) -> int:
    """落库评分回测结果（按 run_date×source×tier 覆盖）。evals 为 pool_eval 引擎输出。"""
    if not evals:
        return 0
    init_db()
    n = 0
    with _conn() as con:
        for e in evals:
            for tier, st in (e.get("tiers") or {}).items():
                con.execute(
                    "INSERT INTO pool_eval (run_date, source, tier, n, win_rate, avg_return) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(run_date, source, tier) DO UPDATE SET "
                    "n=excluded.n, win_rate=excluded.win_rate, avg_return=excluded.avg_return",
                    (e["run_date"], e["source"], tier, st.get("n"),
                     st.get("win_rate"), st.get("avg_return")))
                n += 1
    return n


def load_evals(source: str = "backtest") -> list[dict]:
    """读评分回测结果，按日期升序聚合成 [{run_date, source, tiers:{tier:{n,win_rate,avg_return}}}]。"""
    init_db()
    with _conn() as con:
        rows = con.execute(
            "SELECT run_date, tier, n, win_rate, avg_return FROM pool_eval "
            "WHERE source=? ORDER BY run_date", (source,)).fetchall()
    by_date: dict[str, dict] = {}
    for r in rows:
        d = by_date.setdefault(r["run_date"], {"run_date": r["run_date"], "source": source, "tiers": {}})
        d["tiers"][r["tier"]] = {"n": r["n"], "win_rate": r["win_rate"], "avg_return": r["avg_return"]}
    return list(by_date.values())


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


# ──────────────────────────────────────────────
# 自选/持仓（watchlist）：自选盯盘 + 持仓盈亏
# ──────────────────────────────────────────────

_WATCH_FIELDS = ("name", "is_holding", "cost", "shares", "stop_loss", "target_price", "note")


WATCH_OWNERS = ("me", "dad")            # 用户1=我 / 用户2=爸爸（都由我在网站操作·只是分区归属）


def add_watch(ts_code: str, name: str = "", *, is_holding: bool = False,
              cost: float | None = None, shares: float | None = None,
              stop_loss: float | None = None, target_price: float | None = None,
              note: str = "", owner: str = "me") -> None:
    """加入某人的自选/持仓（按 (owner, ts_code) upsert：已存在则更新非空字段，保留 added_at）。"""
    init_db()
    with _conn() as con:
        con.execute(
            """INSERT INTO watchlist (owner, ts_code, name, is_holding, cost, shares, stop_loss, target_price, note)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(owner, ts_code) DO UPDATE SET
                 name=excluded.name, is_holding=excluded.is_holding, cost=excluded.cost,
                 shares=excluded.shares, stop_loss=excluded.stop_loss,
                 target_price=excluded.target_price, note=excluded.note,
                 updated_at=datetime('now','localtime')""",
            (owner, ts_code, name, int(is_holding), cost, shares, stop_loss, target_price, note),
        )


def update_watch(ts_code: str, *, owner: str = "me", **fields) -> bool:
    """更新某人某条自选/持仓的部分字段（只允许 _WATCH_FIELDS）。返回是否命中。"""
    cols = {k: v for k, v in fields.items() if k in _WATCH_FIELDS}
    if not cols:
        return False
    if "is_holding" in cols:
        cols["is_holding"] = int(bool(cols["is_holding"]))
    sets = ", ".join(f"{k}=?" for k in cols) + ", updated_at=datetime('now','localtime')"
    init_db()
    with _conn() as con:
        cur = con.execute(f"UPDATE watchlist SET {sets} WHERE owner=? AND ts_code=?",
                          (*cols.values(), owner, ts_code))
        return cur.rowcount > 0


def remove_watch(ts_code: str, *, owner: str = "me") -> bool:
    """移除某人的自选/持仓。返回是否命中。"""
    init_db()
    with _conn() as con:
        return con.execute("DELETE FROM watchlist WHERE owner=? AND ts_code=?",
                           (owner, ts_code)).rowcount > 0


def get_watchlist(owner: str | None = None) -> list[dict]:
    """读取自选/持仓（持仓在前，再按加入时间倒序）。owner=None 读全部(各行带 owner)，否则只读该人。"""
    init_db()
    where, params = ("WHERE owner=?", (owner,)) if owner else ("", ())
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM watchlist {where} ORDER BY is_holding DESC, added_at DESC", params
        ).fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# 盘感训练记分（perception_log）
# ──────────────────────────────────────────────

def log_perception(*, ts_code: str, name: str, t0: str, setup_tag: str, market_state: str,
                   pred: str, actual: str, ret_fwd: float, points: float,
                   direction_right: bool) -> None:
    """记一局盘感训练结果（揭晓后调用）。"""
    init_db()
    with _conn() as con:
        con.execute(
            """INSERT INTO perception_log
               (ts_code, name, t0, setup_tag, market_state, pred, actual, ret_fwd, points, direction_right)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ts_code, name, t0, setup_tag, market_state, pred, actual,
             ret_fwd, points, int(direction_right)),
        )


def perception_stats() -> dict:
    """训练统计：局数/平均分/命中率/方向准确率 + 按大盘状态、按形态分组。"""
    init_db()
    with _conn() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM perception_log").fetchall()]
    return _agg_perception(rows)


def _agg_perception(rows: list[dict]) -> dict:
    """聚合统计（纯函数·可测）。命中率=完全命中档占比；方向准确率=涨/平/跌判对占比。"""
    n = len(rows)
    if not n:
        return {"n": 0}
    exact = sum(1 for r in rows if r["points"] == 1.0)
    avg = round(sum(r["points"] for r in rows) / n, 3)
    dir_ok = sum(1 for r in rows if r["direction_right"])

    def _grp(key: str) -> list[dict]:
        g: dict[str, list[dict]] = {}
        for r in rows:
            g.setdefault(r.get(key) or "?", []).append(r)
        out = [{"key": k, "n": len(v),
                "exact_rate": round(sum(1 for x in v if x["points"] == 1.0) / len(v) * 100, 1),
                "dir_rate": round(sum(1 for x in v if x["direction_right"]) / len(v) * 100, 1)}
               for k, v in g.items()]
        return sorted(out, key=lambda x: -x["n"])

    return {"n": n, "avg_points": avg,
            "exact_rate": round(exact / n * 100, 1),
            "dir_rate": round(dir_ok / n * 100, 1),
            "base_exact": 20.0,            # 5档随机瞎猜 exact≈20%（诚实基准对比）
            "base_dir": 33.0,             # 涨/平/跌随机≈33%
            "by_state": _grp("market_state"), "by_setup": _grp("setup_tag")}


# ──────────────────────────────────────────────
# 交易计划（用户最终决定 → 导出 plan.json 喂 QMT）
# ──────────────────────────────────────────────

_PLAN_FIELDS = ("name", "side", "buy_price", "stop_loss", "take_profit", "position_pct", "note", "status")


def add_plan(ts_code: str, name: str = "", *, side: str = "buy", buy_price: float | None = None,
             stop_loss: float | None = None, take_profit: float | None = None,
             position_pct: float | None = None, note: str = "") -> int:
    """新增一条交易计划（用户最终决定）。返回行 id。"""
    init_db()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO trade_plan (ts_code, name, side, buy_price, stop_loss,
                                       take_profit, position_pct, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ts_code, name, side, buy_price, stop_loss, take_profit, position_pct, note),
        )
        return int(cur.lastrowid)


def update_plan(plan_id: int, **fields) -> bool:
    """更新计划部分字段（只允许 _PLAN_FIELDS）。返回是否命中。"""
    cols = {k: v for k, v in fields.items() if k in _PLAN_FIELDS}
    if not cols:
        return False
    sets = ", ".join(f"{k}=?" for k in cols)
    init_db()
    with _conn() as con:
        return con.execute(f"UPDATE trade_plan SET {sets} WHERE id=?",
                           (*cols.values(), int(plan_id))).rowcount > 0


def remove_plan(plan_id: int) -> bool:
    """删除一条交易计划。返回是否命中。"""
    init_db()
    with _conn() as con:
        return con.execute("DELETE FROM trade_plan WHERE id=?", (int(plan_id),)).rowcount > 0


def list_plans(status: str = "") -> list[dict]:
    """读取交易计划（默认全部，新→旧）。status 非空时按状态过滤。"""
    init_db()
    with _conn() as con:
        if status:
            rows = con.execute("SELECT * FROM trade_plan WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM trade_plan ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# AI 问答会话（多会话 + 消息历史）
# ──────────────────────────────────────────────

def new_chat_session(title: str = "新对话") -> int:
    """新建会话，返回 id。"""
    init_db()
    with _conn() as con:
        cur = con.execute("INSERT INTO chat_sessions (title) VALUES (?)", (title[:60],))
        return int(cur.lastrowid)


def list_chat_sessions(limit: int = 50) -> list[dict]:
    """会话列表（最近更新在前）。"""
    init_db()
    with _conn() as con:
        rows = con.execute(
            "SELECT id, title, updated_at FROM chat_sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_chat_messages(session_id: int, limit: int = 60) -> list[dict]:
    """某会话的消息（时间正序）。"""
    init_db()
    with _conn() as con:
        rows = con.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE session_id=? "
            "ORDER BY id ASC LIMIT ?", (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def add_chat_message(session_id: int, role: str, content: str) -> None:
    """追加一条消息并刷新会话更新时间。"""
    init_db()
    with _conn() as con:
        con.execute("INSERT INTO chat_messages (session_id, role, content) VALUES (?,?,?)",
                    (session_id, role, content))
        con.execute("UPDATE chat_sessions SET updated_at=datetime('now','localtime') WHERE id=?",
                    (session_id,))


def rename_chat_session(session_id: int, title: str) -> None:
    init_db()
    with _conn() as con:
        con.execute("UPDATE chat_sessions SET title=? WHERE id=?", (title[:60], session_id))


def delete_chat_session(session_id: int) -> bool:
    """删除会话及其消息。"""
    init_db()
    with _conn() as con:
        con.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        return con.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,)).rowcount > 0


# ──────────────────────────────────────────────
# 人气榜每日快照（自建轨迹·供人气反转选股算峰值/谷值/回升）
# ──────────────────────────────────────────────

def log_hot_rank(kind: str, rows: list[dict], trade_date: str | None = None) -> int:
    """把人气/飙升榜快照落到 hot_rank_log（幂等·同日同票覆盖）。返回写入条数。

    两条路径共用：
      · 自建路径：每交易日的当日榜 rows=[{code,name,rank,rank_chg?}]，trade_date 缺省=当天。
      · 详情API路径(家用脚本)：一次推多票多日历史，**每行自带 trade_date**（覆盖 day 参数）。
    """
    import datetime
    if not rows:
        return 0
    fallback = trade_date or datetime.date.today().strftime("%Y%m%d")
    init_db()
    n = 0
    with _conn() as con:
        for r in rows:
            code = str(r.get("code") or "").zfill(6)
            if not code or code == "000000":
                continue
            day = str(r.get("trade_date") or fallback)
            con.execute(
                """INSERT OR REPLACE INTO hot_rank_log (trade_date, kind, code, name, rank, rank_chg)
                   VALUES (?,?,?,?,?,?)""",
                (day, kind, code, r.get("name"), _int_or_none(r.get("rank")),
                 _int_or_none(r.get("rank_chg"))),
            )
            n += 1
    return n


def hot_rank_trajectory(kind: str = "rank", days: int = 14) -> list[dict]:
    """从自建日志聚合每票近 N 日人气轨迹：当前/峰值(最小名次)/谷值(最大名次)/回升。

    仅覆盖曾进榜(被记录)的票；未进当日榜的天缺失 → 谷值为"被记录到的最差名次"(偏乐观·会低估洗盘深度)，
    故自建轨迹只作近似，精确 300-800 需家用详情API路径。返回 [{code,name,cur_rank,peak_rank,trough_rank,days_seen}]。
    """
    init_db()
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT code, name, rank, trade_date FROM hot_rank_log
               WHERE kind = ? AND trade_date IN (
                   SELECT DISTINCT trade_date FROM hot_rank_log
                   WHERE kind = ? ORDER BY trade_date DESC LIMIT ?)
               ORDER BY code, trade_date""",
            (kind, kind, int(days))).fetchall()]
    return _agg_hot_trajectory(rows)


def _agg_hot_trajectory(rows: list[dict]) -> list[dict]:
    """聚合成每票轨迹（纯函数·可测）。名次越小越火：峰值=min rank·谷值=max rank·当前=最新日 rank。"""
    by: dict[str, dict] = {}
    for r in rows:
        code = r.get("code")
        rk = r.get("rank")
        if not code or rk is None:
            continue
        g = by.setdefault(code, {"code": code, "name": r.get("name"),
                                 "ranks": [], "last_date": "", "last_rank": None})
        g["ranks"].append(int(rk))
        if str(r.get("trade_date") or "") >= g["last_date"]:
            g["last_date"], g["last_rank"], g["name"] = str(r.get("trade_date")), int(rk), r.get("name")
    out = []
    for g in by.values():
        if not g["ranks"]:
            continue
        out.append({"code": g["code"], "name": g["name"], "cur_rank": g["last_rank"],
                    "peak_rank": min(g["ranks"]), "trough_rank": max(g["ranks"]),
                    "days_seen": len(g["ranks"])})
    return out


def _int_or_none(v):
    try:
        return int(v) if v is not None and str(v).strip() not in ("", "nan") else None
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────
# 认知脚手架：每日「5问框架」推演日志（练框架·事后校准）
# ──────────────────────────────────────────────

_COG_FIELDS = ("q1_regime", "q2_mainline", "q3_tempo", "q4_catalyst", "q5_path",
               "stance", "main_line", "confidence", "sh_close")


def save_cognition(trade_date: str, entry: dict) -> None:
    """存/更一天的推演日志（每交易日一条·再存即覆盖当日·保留 created_at）。"""
    init_db()
    vals = {k: entry.get(k) for k in _COG_FIELDS}
    with _conn() as con:
        exists = con.execute("SELECT 1 FROM cognition_log WHERE trade_date=?", (trade_date,)).fetchone()
        if exists:
            sets = ", ".join(f"{k}=?" for k in _COG_FIELDS)
            con.execute(f"UPDATE cognition_log SET {sets}, updated_at=datetime('now','localtime') "
                        f"WHERE trade_date=?", (*[vals[k] for k in _COG_FIELDS], trade_date))
        else:
            cols = ", ".join(("trade_date", *_COG_FIELDS))
            ph = ", ".join(["?"] * (len(_COG_FIELDS) + 1))
            con.execute(f"INSERT INTO cognition_log ({cols}) VALUES ({ph})",
                        (trade_date, *[vals[k] for k in _COG_FIELDS]))


def get_cognition(trade_date: str) -> dict | None:
    """取某日推演日志（供当日续填/编辑）。"""
    init_db()
    with _conn() as con:
        r = con.execute("SELECT * FROM cognition_log WHERE trade_date=?", (trade_date,)).fetchone()
    return dict(r) if r else None


def list_cognition(limit: int = 60) -> list[dict]:
    """近 N 条推演日志（倒序·供回看校准）。"""
    init_db()
    with _conn() as con:
        rows = con.execute("SELECT * FROM cognition_log ORDER BY trade_date DESC LIMIT ?",
                           (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def update_cognition_review(trade_date: str, note: str) -> bool:
    """回看时补自评（哪问看对/看错）。"""
    init_db()
    with _conn() as con:
        cur = con.execute("UPDATE cognition_log SET review_note=?, updated_at=datetime('now','localtime') "
                          "WHERE trade_date=?", (note, trade_date))
        return cur.rowcount > 0
