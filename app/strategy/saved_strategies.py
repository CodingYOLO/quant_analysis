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


_PRESET_CREATOR = "系统预设"

# 内置实战策略（贴合 A 股当前高位分歧·科技主线轮动行情）。
# payload 与前端一致：factors(因子键) / customs([{col,op,val}]) / custom(近N日) / sort_by。
PRESET_STRATEGIES: list[dict] = [
    {
        "name": "① 主升浪龙头(强势追涨)",
        "desc": "均线多头+强相对强度+主力进场，主线趋势行情追龙头",
        "payload": {
            "factors": ["pat_ma_bull_stack", "rps_ge80", "main_inflow", "amount_ge1"],
            "customs": [{"col": "turnover_rate", "op": "ge", "val": 3},
                        {"col": "circ_mv_100m", "op": "ge", "val": 50}],
            "custom": None, "sort_by": "rps120",
        },
    },
    {
        "name": "② 缩量回踩低吸(吴川核心)",
        "desc": "强势股缩量回踩MA20企稳、资金仍流入、未过热，上升趋势买回调",
        "payload": {
            "factors": ["pat_shrink_pullback_ma20", "above_ma20", "above_ma60",
                        "main_inflow", "rps50_ge70"],
            "customs": [], "custom": {"n": 7, "op": "le", "val": 12}, "sort_by": "rps50",
        },
    },
    {
        "name": "③ 放量突破前高(突破追击)",
        "desc": "收盘创20日新高+放量+主力+强度，突破启动右侧介入",
        "payload": {
            "factors": ["pat_breakout_high_20", "main_inflow", "vol_ratio_ge15"],
            "customs": [{"col": "turnover_rate", "op": "ge", "val": 3},
                        {"col": "circ_mv_100m", "op": "ge", "val": 50},
                        {"col": "rps120", "op": "ge", "val": 80}],
            "custom": None, "sort_by": "rps120",
        },
    },
    {
        "name": "④ 九转见底超跌反弹(分歧市防守低吸)",
        "desc": "TD神奇九转见底+长下影承接+中期均线未破+RSI低位，调整市抢反弹",
        "payload": {
            "factors": ["td_buy9", "long_lower", "above_ma60"],
            "customs": [{"col": "rsi14", "op": "le", "val": 45},
                        {"col": "circ_mv_100m", "op": "ge", "val": 50}],
            "custom": None, "sort_by": "main_net_amount",
        },
    },
    {
        "name": "⑤ 趋势启动·均线金叉(右侧)",
        "desc": "MACD金叉+EMA多头+站上MA20+资金+强度，趋势初期右侧",
        "payload": {
            "factors": ["macd_gold", "ema_bull", "above_ma20", "main_inflow", "rps50_ge70"],
            "customs": [{"col": "turnover_rate", "op": "ge", "val": 2}],
            "custom": None, "sort_by": "rps50",
        },
    },
    {
        "name": "⑥ 主力吸筹·量价齐升(资金驱动)",
        "desc": "量价齐升+超大单净流入+站上MA20，资金主导行情",
        "payload": {
            "factors": ["pat_vol_price_surge", "elg_inflow", "above_ma20"],
            "customs": [{"col": "turnover_rate", "op": "ge", "val": 3},
                        {"col": "circ_mv_100m", "op": "ge", "val": 50}],
            "custom": None, "sort_by": "elg_net",
        },
    },
]


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
                payload    TEXT NOT NULL,                 -- json: {factors, customs, custom, sort_by, desc}
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(name, creator)                     -- 同一创建者同名覆盖
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_strat_creator ON saved_strategies(creator)")
        con.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)")


# 预设版本号：升版本即重新播种新一批预设（已存在的同名不覆盖、用户删过的不复活）
_PRESET_SEED_KEY = "presets_seeded_v1"


def seed_presets() -> int:
    """
    一次性播种内置实战策略（幂等）。用 _meta 标记，仅首次播种；
    用户删除的预设不会自动复活。返回本次新增条数。
    """
    init()
    with _conn() as con:
        done = con.execute("SELECT value FROM _meta WHERE key=?", (_PRESET_SEED_KEY,)).fetchone()
        if done:
            return 0
        n = 0
        for p in PRESET_STRATEGIES:
            payload = dict(p["payload"])
            payload["desc"] = p.get("desc", "")
            cur = con.execute(
                "INSERT OR IGNORE INTO saved_strategies (name, creator, payload) VALUES (?,?,?)",
                (p["name"], _PRESET_CREATOR, json.dumps(payload, ensure_ascii=False)),
            )
            n += cur.rowcount
        con.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    (_PRESET_SEED_KEY, "1"))
    return n


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
    seed_presets()        # 首次自动播种内置实战策略（幂等）
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
