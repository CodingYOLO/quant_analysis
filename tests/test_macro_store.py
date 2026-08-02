"""宏观面板 · 存储层与注册表测试（建表幂等 / WAL / 元数据可调项保留 / point-in-time 读取）。

运行：.venv/bin/python tests/test_macro_store.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.macro.store as store          # noqa: E402
from app.macro import registry           # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _use_temp_db(tmp: Path) -> None:
    """把库指向临时目录，避免污染真实 data_cache。"""
    store.db_path = lambda: tmp / "macro.db"          # type: ignore[assignment]


# ── 建表幂等 + WAL 必须真的开起来 ──────────────────────────────────────────
def test_init_idempotent_and_wal() -> None:
    with tempfile.TemporaryDirectory() as d:
        _use_temp_db(Path(d))
        store.init_db()
        store.init_db()                                # 重复调用必须安全
        with store._conn() as con:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        for t in ("metric_meta", "macro_daily", "macro_run_log", "macro_calendar", "macro_summary"):
            _assert(t in tables, f"缺表 {t}")
        _assert(mode.lower() == "wal",
                f"⭐必须是 WAL(cron写+web读并发)，实为 {mode}")
    print("  ✓ 建表幂等 · WAL 已开启")


# ── 注册表自检 ────────────────────────────────────────────────────────────
def test_registry_valid() -> None:
    problems = registry.validate()
    _assert(not problems, "注册表自检失败:\n  " + "\n  ".join(problems))
    codes = [m.code for m in registry.METRICS]
    _assert(len(codes) == len(set(codes)), "指标 code 有重复")
    # dxy/vix：数据已验证可得(东财 100.UDI / 167.VIX 服务器实测 HTTP200)，
    # 但服务器取数通道待定 → 本期 enabled=0，作独立小任务，不阻塞主线
    dxy = registry.get("dxy")
    _assert(dxy is not None and not dxy.enabled, "dxy 本期应停用(取数通道待定)")
    _assert("100.UDI" in dxy.api, f"dxy 的 secid 结论应保留在 api 字段，实为 {dxy.api}")
    _assert(dxy.source_fallback, "dxy 应配备源")
    vix = registry.get("vix")
    _assert(vix is not None and not vix.enabled and "167.VIX" in vix.api,
            "vix 本期应停用，但 secid 结论保留")
    # OMO 系统性排查后确无源，必须保持停用而不是塞个替代品
    omo = registry.get("omo_net")
    _assert(omo is not None and not omo.enabled, "omo_net 无可靠日频源，必须 enabled=False")
    # commit 4 定档回归：
    fr = registry.get("float_release")
    _assert(fr.no_dist == 1 and fr.direction == 0,
            "⭐(c) 解禁=前瞻计划·无历史分布 → no_dist=1 且 direction=0(纯展示)")
    bb = registry.get("buyback_amt")
    _assert(bb is not None and not bb.enabled and "累计" in bb.note,
            "回购 amount 为程序内累计值且无程序ID·必须停用并写明证据")
    mf = registry.get("mainflow_total")
    _assert("mkt_dc" in mf.api and "吻合" in mf.note,
            "mainflow 换 mkt_dc 必须带与项目口径的逐日验证记录")
    # 层归属：国内情绪不许塞进"外部输入"
    qvix = registry.get("qvix_300")
    _assert(qvix is not None and qvix.layer == "L2_sentiment",
            "⭐qvix 属国内情绪，必须归 L2 而非 L3")
    print("  ✓ 注册表自检通过（%d 个指标，启用 %d 个）"
          % (len(codes), len(registry.enabled_codes())))


# ── 断点配置：语义断点硬截断 / 制度断点只标注 ──────────────────────────────
def test_break_config() -> None:
    nb = registry.get("northbound_turnover")
    _assert(nb.hist_break == "20240819", "北向断点日应为 20240819")
    _assert(nb.break_mode == "truncate", "⭐北向是语义断点(净买入→成交额)，必须硬截断")
    _assert(nb.direction == 0, "北向只作活跃度代理，不判多空")
    for code in ("margin_ratio", "margin_balance"):
        m = registry.get(code)
        _assert(m.hist_break == "20230911,20260119",
                f"{code} 应含两个保证金比例断点，实为 {m.hist_break}")
        _assert(m.break_mode == "mark", f"{code} 是制度断点，默认只标注不截断")
    _assert(registry.get("margin_balance").direction == 0,
            "融资余额绝对值单边趋势·不参与评分")
    _assert(registry.get("margin_ratio").direction == 1,
            "融资余额比值才是参与评分的主指标")
    print("  ✓ 断点配置：语义断点截断 / 制度断点标注")


# ── score_from：mark 模式必须配套，否则评分把制度性下移读成情绪降温 ──────────
def test_score_from_guards_mark_mode() -> None:
    for m in registry.METRICS:
        if m.break_mode == "mark" and m.hist_break and m.direction != 0:
            _assert(m.score_from, f"⭐{m.code}: break_mode=mark 且参与评分，必须设 score_from——"
                                  "竖线只给人看，layer_score 看不见竖线，会把制度性中枢下移"
                                  "误读成情绪降温，导致该层得分虚高数月")
            last_break = max(filter(None, m.hist_break.split(",")))
            _assert(m.score_from > last_break,
                    f"{m.code}: score_from({m.score_from}) 必须晚于最近断点({last_break})")
    mr = registry.get("margin_ratio")
    _assert(mr.score_from == "20260419",
            f"margin_ratio score_from 应为断点20260119+3个月，实为 {mr.score_from}")
    print("  ✓ score_from：mark 模式已配套评分门禁")


# ── 元数据可调项：代码改注册表不得冲掉用户在库里的手工调整 ──────────────────
def test_meta_tuning_preserved() -> None:
    with tempfile.TemporaryDirectory() as d:
        _use_temp_db(Path(d))
        registry.sync_to_db()
        with store._conn() as con:                     # 模拟用户手工调整
            con.execute("UPDATE metric_meta SET weight=3.5, enabled=0 WHERE code='sox'")
        registry.sync_to_db()                          # 再同步一次注册表
        rows = {r["code"]: r for r in store.get_meta(enabled_only=False)}
        _assert(abs(rows["sox"]["weight"] - 3.5) < 1e-9,
                "⭐用户调过的 weight 必须保留，不能被注册表默认值冲掉")
        _assert(rows["sox"]["enabled"] == 1,
                "⭐enabled 编码的是『有没有可用数据源』(代码侧事实)，必须跟随 registry；"
                "否则源接上了/没了都推不下去。用户要停用应设 weight=0")
        _assert("费城半导体" in rows["sox"]["name_cn"], "定义类字段(name_cn)应随注册表更新")
        store.reset_meta_tuning(["sox"])               # 显式重置才恢复默认
        rows = {r["code"]: r for r in store.get_meta(enabled_only=False)}
        _assert(abs(rows["sox"]["weight"] - 2.0) < 1e-9, "reset 后应回到注册表默认 weight=2.0")
    # carry 默认值经 as_row 解析(-1→按频率)：daily=2会话·monthly=45自然日
    _assert(rows["fdr007"]["max_carry_days"] == 2, "daily 默认结转=2个交易日会话")
    _assert(rows["cpi_yoy"]["max_carry_days"] == 45, "monthly 默认结转=45自然日")
    print("  ✓ 元数据：可调项保留 · 定义字段跟随 · 显式 reset 生效")


# ── point-in-time：读序列绝不能返回 end_date 之后的数据 ────────────────────
def test_read_series_point_in_time() -> None:
    with tempfile.TemporaryDirectory() as d:
        _use_temp_db(Path(d))
        store.init_db()
        store.upsert_daily([
            {"trade_date": f"2026070{i}", "code": "fdr007", "value": float(i), "as_of": f"2026070{i}"}
            for i in range(1, 8)
        ])
        got = store.read_series("fdr007", "20260704")
        _assert([r["trade_date"] for r in got] == ["20260701", "20260702", "20260703", "20260704"],
                f"⭐回看模式必须严格 point-in-time，实得 {[r['trade_date'] for r in got]}")
        _assert(store.latest_date() == "20260707", "latest_date 应为库中最大日")
        store.upsert_daily([{"trade_date": "20260704", "code": "fdr007", "value": 99.0}])
        _assert(store.read_series("fdr007", "20260704")[-1]["value"] == 99.0, "同键应覆盖更新")
    print("  ✓ 读序列 point-in-time · 同键覆盖")


# ── NULL 语义：取不到就写 NULL，绝不能变成 0 ──────────────────────────────
def test_null_not_zero() -> None:
    with tempfile.TemporaryDirectory() as d:
        _use_temp_db(Path(d))
        store.init_db()
        store.upsert_daily([{"trade_date": "20260731", "code": "margin_balance",
                             "value": None, "as_of": None}])
        row = store.read_panel("20260731")["margin_balance"]
        _assert(row["value"] is None, "⭐未取到必须是 NULL，不能是 0(0 会被当成真实值)")
        store.log_runs([{"run_id": "r1", "trade_date": "20260731", "code": "margin_balance",
                         "status": "partial", "err_msg": "SZSE/BSE 未发布"}])
        log = store.read_run_log("20260731")
        _assert(log and log[0]["status"] == "partial", "缺源必须落 run_log 供告警")
    print("  ✓ NULL 语义 · 失败落 run_log")


if __name__ == "__main__":
    print("宏观面板 · 存储层与注册表测试")
    for fn in (test_init_idempotent_and_wal, test_registry_valid, test_break_config,
               test_score_from_guards_mark_mode, test_meta_tuning_preserved,
               test_read_series_point_in_time, test_null_not_zero):
        fn()
    print("✅ 全部通过")
