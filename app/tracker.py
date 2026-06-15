"""
个股持仓追踪器（O15）。

功能：
- 记录入选日期，关联入选价格（保守买入价）
- 每日计算浮盈/浮亏（%）
- 到达止盈/止损价时在报告里标注提醒
- 单只股票最多追踪 10 个交易日（防过期噪声）

数据库：与 history_tracker 共用 data_cache/history.db，但用独立表。
"""

import logging
import sqlite3
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

_DB_FILENAME = "history.db"
_MAX_TRACK_DAYS = 10  # 超过10个交易日自动停止追踪


def _get_db_path() -> Path:
    settings = get_settings()
    return settings.cache_dir / _DB_FILENAME


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_get_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def init_tracking_table() -> None:
    """创建追踪表（幂等）。"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS position_tracking (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code         TEXT NOT NULL,
                name            TEXT NOT NULL,
                theme           TEXT,
                entry_date      TEXT NOT NULL,      -- 入选日 YYYYMMDD
                entry_price     REAL,               -- 保守买入价
                stop_loss       REAL,               -- 止损价
                take_profit_1   REAL,               -- 止盈1
                take_profit_2   REAL,               -- 止盈2
                status          TEXT DEFAULT 'tracking',  -- tracking / stopped / hit_tp1 / hit_tp2 / stopped_out
                last_update     TEXT,               -- 最后更新日 YYYYMMDD
                track_days      INTEGER DEFAULT 0,
                latest_price    REAL,
                float_pct       REAL,               -- 最新浮盈/亏 %
                created_at      TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_tracking_stock
                ON position_tracking(ts_code, entry_date);
        """)


def add_tracking(
    trade_date: str,
    ts_code: str,
    name: str,
    theme: str,
    entry_price: float,
    stop_loss: float,
    take_profit_1: float,
    take_profit_2: float,
) -> None:
    """
    将通过辩论的候选股加入追踪（以保守买入价为基准）。
    同一只股票同一天只记录一次。
    """
    init_tracking_table()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO position_tracking
                (ts_code, name, theme, entry_date, entry_price, stop_loss,
                 take_profit_1, take_profit_2, last_update)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts_code, name, theme, trade_date, entry_price,
             stop_loss, take_profit_1, take_profit_2, trade_date),
        )


def update_tracking(trade_date: str, price_map: dict[str, float]) -> list[dict]:
    """
    用当日收盘价更新所有追踪中股票的浮盈/亏，并检测止盈止损触发。

    Returns:
        触发止盈/止损的股票列表（用于报告标注）
    """
    if not price_map:
        return []

    init_tracking_table()
    alerts: list[dict] = []

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM position_tracking WHERE status='tracking'"
        ).fetchall()

        for row in rows:
            current = price_map.get(row["ts_code"])
            if current is None or row["entry_price"] is None:
                continue

            float_pct = round((current / row["entry_price"] - 1) * 100, 2)
            track_days = row["track_days"] + 1
            new_status = "tracking"

            # 止盈止损判断
            if row["stop_loss"] and current <= row["stop_loss"]:
                new_status = "stopped_out"
            elif row["take_profit_2"] and current >= row["take_profit_2"]:
                new_status = "hit_tp2"
            elif row["take_profit_1"] and current >= row["take_profit_1"]:
                new_status = "hit_tp1"
            elif track_days >= _MAX_TRACK_DAYS:
                new_status = "stopped"

            conn.execute(
                """
                UPDATE position_tracking
                SET latest_price=?, float_pct=?, track_days=?,
                    status=?, last_update=?
                WHERE id=?
                """,
                (current, float_pct, track_days, new_status, trade_date, row["id"]),
            )

            if new_status in ("stopped_out", "hit_tp1", "hit_tp2"):
                alerts.append({
                    "ts_code": row["ts_code"],
                    "name": row["name"],
                    "status": new_status,
                    "entry_price": row["entry_price"],
                    "current_price": current,
                    "float_pct": float_pct,
                    "entry_date": row["entry_date"],
                })

    if alerts:
        logger.info("[持仓追踪] %d 只股票触发止盈/止损提醒", len(alerts))
    return alerts


def get_active_tracking() -> list[dict]:
    """获取当前追踪中的所有股票。"""
    init_tracking_table()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM position_tracking WHERE status='tracking' ORDER BY entry_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_tracking_alerts_section(trade_date: str, price_map: dict[str, float]) -> str:
    """
    生成报告里的追踪提醒区块文字（供节点E调用）。

    Returns:
        Markdown 文字，空字符串表示无需显示。
    """
    alerts = update_tracking(trade_date, price_map)
    active = get_active_tracking()

    if not alerts and not active:
        return ""

    lines = ["", "## 持仓追踪（O15）"]

    if alerts:
        lines += ["", "### ⚡ 止盈/止损触发提醒"]
        for a in alerts:
            status_label = {
                "stopped_out": "🛑 触发止损",
                "hit_tp1": "🎯 触达止盈1",
                "hit_tp2": "🎯🎯 触达止盈2",
            }.get(a["status"], a["status"])
            lines.append(
                f"- **{a['name']}**({a['ts_code'][:6]}) {status_label}　"
                f"入选价 {a['entry_price']:.2f} → 现价 {a['current_price']:.2f}　"
                f"浮盈 **{a['float_pct']:+.1f}%**（入选日：{a['entry_date']}）"
            )

    if active:
        lines += ["", "### 📋 追踪中个股（持续跟进）"]
        lines += [
            "",
            "| 名称 | 代码 | 入选日 | 入选价 | 现价 | 浮盈/亏 | 止损 | 止盈1 | 天数 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for a in active:
            float_str = f"**{a['float_pct']:+.1f}%**" if a.get("float_pct") else "—"
            latest = f"{a['latest_price']:.2f}" if a.get("latest_price") else "—"
            lines.append(
                f"| {a['name']} | {a['ts_code'][:6]} | {a['entry_date']} "
                f"| {a['entry_price']:.2f} | {latest} | {float_str} "
                f"| {a['stop_loss']:.2f} | {a['take_profit_1']:.2f} | {a['track_days']} |"
            )

    return "\n".join(lines)
