"""宏观面板 · 服务层测试（临时库夹具·不打网络）。

commit 5 三条要求逐一守住：
(1) 未启用/待接入层给明确 state——绝不渲染成 0 分；
(2) 面板只读库（本测试全程无网络即为证明）；
(3) 卡片带 as_of / sample_n / 距下次发布。

运行：.venv/bin/python tests/test_macro_service.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.macro.store as store              # noqa: E402
from app.macro import registry, service      # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _seed(tmp: Path) -> None:
    store.db_path = lambda: tmp / "macro.db"      # type: ignore[assignment]
    registry.sync_to_db()
    rows = []
    for i, d in enumerate(("20260729", "20260730", "20260731")):
        rows += [
            {"trade_date": d, "code": "fdr007", "value": 1.4 + i * 0.01, "chg_1d": 0.01,
             "pctile_750": 40 + i, "sample_n": 750, "as_of": d, "is_stale": 0, "anomaly": 0},
            {"trade_date": d, "code": "cpi_yoy", "value": 0.2, "pctile_750": 30.0,
             "sample_n": 36, "as_of": "20260630", "is_stale": 20 + i, "anomaly": 0},
            {"trade_date": d, "code": "margin_ratio", "value": 2.7, "pctile_750": 94.0,
             "sample_n": 590, "as_of": d, "is_stale": 0,
             "anomaly": 1 if d == "20260731" else 0},
            {"trade_date": d, "code": "float_release", "value": 1074.0, "pctile_750": None,
             "sample_n": None, "as_of": d, "is_stale": 0, "anomaly": 0},
            {"trade_date": d, "code": "usdcnh", "value": None, "as_of": "20260728",
             "is_stale": 3, "anomaly": 0},        # 断供3会话→NULL(数据未更新)
        ]
    store.upsert_daily(rows)
    # anomaly 列走 compute 的真实写入路径(update_derived)——upsert_daily 有意不含它
    store.update_derived([{"trade_date": "20260731", "code": "margin_ratio",
                           "chg_1d": 0.02, "chg_5d": 0.05, "zscore_250": 2.3,
                           "pctile_750": 94.0, "sample_n": 590, "anomaly": 1}])
    store.upsert_scores([
        {"trade_date": "20260731", "layer": "L0_liquidity", "score": 64.2, "n_part": 15, "n_total": 15},
        {"trade_date": "20260731", "layer": "L1_flow", "score": 69.2, "n_part": 6, "n_total": 6},
        {"trade_date": "20260731", "layer": "L2_sentiment", "score": None, "n_part": 0, "n_total": 0},
        {"trade_date": "20260731", "layer": "L3_external", "score": None, "n_part": 0, "n_total": 3},
        {"trade_date": "20260731", "layer": "TOTAL", "score": 66.7, "n_part": 2, "n_total": 4},
    ])


def test_panel() -> None:
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td))
        p = service.build_panel("")
        _assert(p["ok"] and p["date"] == "20260731", "默认取库内最新日")

        # (1) 层状态：L2 未启用 / L3 待接入 —— 都不是 0 分
        st = {x["layer"]: x["state"] for x in p["layers"]}
        _assert(st["L2_sentiment"] == "inactive", f"⭐L2 应为『该层未启用』·实为 {st['L2_sentiment']}")
        _assert(st["L3_external"] == "pending", f"⭐L3 启用但无数据·应为『待接入』·实为 {st['L3_external']}")
        tiles = {t["layer"]: t for t in p["thermo"]["tiles"]}
        _assert("score" not in tiles["L2_sentiment"], "未启用层的体温计块不得携带任何 score 字段")
        _assert(p["thermo"]["total"]["score"] == 66.7, "总分照常显示")
        _assert("已启用层均值" in p["thermo"]["total"]["note"], "总分必须注明只覆盖已启用层")

        cards = {c["code"]: c for lay in p["layers"] for c in lay["cards"]}
        # (3) 三要素：as_of / sample_n / 距下次发布
        cpi = cards["cpi_yoy"]
        _assert(cpi["as_of"] == "20260630", "as_of=真实数据时点(6月报)")
        _assert(cpi["sample_n"] == 36 and cpi["sample_win"] == 36, "月频样本 36/36")
        _assert("pub" in cpi and cpi["pub"]["next_days"] is not None,
                "⭐月频卡必须给『距下次发布约N天』——否则分数横盘分不清是环境没变还是指标不会动")
        f = cards["fdr007"]
        _assert(f["sample_n"] == 750 and f["sample_win"] == 750, "日频样本 750/750")
        _assert("pub" not in f, "日频无『下次发布』概念")

        # 好坏色语义：fdr007 利空(direction=-1)·分位42 → adj=58(偏利好侧)
        _assert(abs(f["adj_pctile"] - 58) < 1e-6, "利空指标 adj=100-pctile(颜色语义=对A股好坏)")

        # 前瞻展示项 & 断供
        fr = cards["float_release"]
        _assert(fr["no_dist"] == 1 and not fr["scored"] and "前瞻" in fr["score_note"],
                "解禁：no_dist 不评分且给出原因")
        u = cards["usdcnh"]
        _assert(u["state"] == "missing" and "0" in u["note"],
                "断供指标=『数据未更新』·明示不显示0/不显示昨值")

        # 断点标注文字（竖线之外必须有字）
        mr = cards["margin_ratio"]
        _assert("制度断点" in mr["break_note"] and "20260119" in mr["break_note"],
                "⭐两融卡必须有『窗口内含制度断点 20260119』文字标注")
        _assert(mr["scored"], "score_from=20260419 已过·margin_ratio 恢复计分")

        # 异动区
        _assert(len(p["anomalies"]) == 1 and p["anomalies"][0]["code"] == "margin_ratio",
                "异动区只列触发的指标")
    print("  ✓ 面板：层状态/三要素/好坏色/前瞻/断供/断点标注/异动 全部符合定档")


def test_lookback_point_in_time() -> None:
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td))
        p = service.build_panel("20260730")
        _assert(p["date"] == "20260730" and p["is_lookback"], "回看模式标记")
        cards = {c["code"]: c for lay in p["layers"] for c in lay["cards"]}
        _assert(len(cards["fdr007"]["spark"]) == 2, "⭐回看日的 sparkline 不含之后的数据点")
        _assert(not cards["margin_ratio"].get("anomaly"), "0731 的异动不得出现在 0730 面板")
        p2 = service.build_panel("20260801")          # 非交易日 → 解析到 ≤ 的最近有数日
        _assert(p2["date"] == "20260731", "非交易日解析到最近有数日")
    print("  ✓ 回看：point-in-time·非交易日解析")


if __name__ == "__main__":
    print("宏观面板 · 服务层测试")
    for fn in (test_panel, test_lookback_point_in_time):
        fn()
    print("✅ 全部通过")
