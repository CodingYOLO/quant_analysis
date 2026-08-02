"""面板组装（**只读 macro.db**·请求路径零外部调用——断网后页面照常打开）。

三条 commit 5 要求（2026-08-02 定档）在此落实：
(1) L2/L3 给出明确的层状态 inactive/pending——**绝不渲染成空仪表盘或 0 分**，
    "外部环境 0 分"和"外部层没接入"是完全不同的信息；
(2) 最小可开版本：体温计 + 异动区 + 卡片区 + 回看模式（链路图/LLM 摘要在 commit 6）；
(3) 每张卡必须带：as_of 数据时点 · sample_n("样本 457/750") · 距下次发布约 N 天。

回看模式的 point-in-time 由数据层保证（macro_daily 每行都是当日真实可得口径·
分位/评分均为右端滚动），本层只做"日期解析到 ≤date 的最近有数日"。
"""

from __future__ import annotations

import logging

from app.macro import store
from app.macro.compute import WINDOWS, publication_status

logger = logging.getLogger(__name__)

_SPARK_N = 60          # sparkline 取近 60 个交易日

# 未激活层的说明（(1)：明确"没接入"而不是"0分"）
_LAYER_PENDING_NOTE = {
    "L2_sentiment": "Phase 2 接入——将复用「🌡️大盘情绪」页现有口径，避免同一个涨停家数出现两个数",
    "L3_external": "数据源待接入（SOX/纳指/恒生科技·东财限流已列独立任务）",
}


def resolve_date(date: str | None) -> str:
    """解析回看日期：取 ≤date 的最近有数据交易日；空/超前 → 库内最新日。"""
    latest = store.latest_date()
    if not date or not latest:
        return latest
    date = date.replace("-", "")[:8]
    if date >= latest:
        return latest
    with store._conn() as con:
        row = con.execute("SELECT MAX(trade_date) AS d FROM macro_daily WHERE trade_date<=?",
                          (date,)).fetchone()
    return (row["d"] or latest) if row else latest


def build_panel(date: str | None = None) -> dict:
    """面板全量数据（体温计/异动/分层卡片/回看日期表）。"""
    store.init_db()
    d = resolve_date(date)
    if not d:
        return {"ok": False, "error": "宏观库为空：请先运行 macro-backfill"}
    metas = store.get_meta(enabled_only=False)
    day_rows = store.read_panel(d)
    layers = [_build_layer(layer, metas, day_rows, d) for layer in store.LAYERS]
    return {
        "ok": True,
        "date": d,
        "is_lookback": bool(date) and d != store.latest_date(),
        "dates": store.available_dates(90),
        "thermo": _build_thermo(layers, d),
        "anomalies": _build_anomalies(metas, day_rows),
        "layers": layers,
    }


# ──────────────────────────────────────────────
# 体温计（分层得分 + 5日趋势）
# ──────────────────────────────────────────────

def _build_thermo(layers: list[dict], d: str) -> dict:
    tiles = []
    for lay in layers:
        t = {"layer": lay["layer"], "label": lay["label"], "state": lay["state"]}
        if lay["state"] == "active":
            srs = store.read_score_series(lay["layer"], d, limit=6)
            cur = srs[-1] if srs else None
            t["score"] = cur["score"] if cur else None
            t["n_part"] = cur["n_part"] if cur else 0
            t["n_total"] = cur["n_total"] if cur else 0
            t["trend5"] = _trend(srs)
        tiles.append(t)
    total_srs = store.read_score_series("TOTAL", d, limit=6)
    cur = total_srs[-1] if total_srs else None
    active_labels = [x["label"] for x in tiles if x["state"] == "active"]
    return {"tiles": tiles,
            "total": {"score": cur["score"] if cur else None, "trend5": _trend(total_srs),
                      # 诚实标注：总分只覆盖已激活层，不是"四层全貌"
                      "note": "总分=已启用层均值（当前：" + "、".join(active_labels) + "）"}}


def _trend(series: list[dict]) -> float | None:
    """近5日得分变化（首尾差·供趋势小箭头）。有效点不足2个 → None。"""
    vals = [s["score"] for s in series if s.get("score") is not None]
    return round(vals[-1] - vals[0], 1) if len(vals) >= 2 else None


# ──────────────────────────────────────────────
# 异动区
# ──────────────────────────────────────────────

def _build_anomalies(metas: list[dict], day_rows: dict) -> list[dict]:
    out = []
    by = {m["code"]: m for m in metas}
    for code, r in day_rows.items():
        if not r.get("anomaly"):
            continue
        m = by.get(code, {})
        out.append({"code": code, "name": m.get("name_cn", code), "unit": m.get("unit", ""),
                    "value": r.get("value"), "chg_1d": r.get("chg_1d"),
                    "pctile": r.get("pctile_750"), "dir": int(r["anomaly"])})
    return sorted(out, key=lambda x: -abs(x.get("chg_1d") or 0))


# ──────────────────────────────────────────────
# 分层卡片
# ──────────────────────────────────────────────

def _build_layer(layer: str, metas: list[dict], day_rows: dict, d: str) -> dict:
    mine = [m for m in metas if m["layer"] == layer]
    enabled = [m for m in mine if m["enabled"]]
    cards = [_build_card(m, day_rows.get(m["code"]), d) for m in enabled]
    has_data = any(c["state"] != "pending_source" for c in cards)
    if not enabled:
        state = "inactive"          # 该层未启用（一个指标都没开）
    elif not has_data:
        state = "pending"           # 启用了但数据源尚未接入
    else:
        state = "active"
    return {
        "layer": layer, "label": store.LAYER_LABELS[layer], "state": state,
        "note": _LAYER_PENDING_NOTE.get(layer, "") if state != "active" else "",
        "cards": cards if state == "active" else [],
        "pending_names": [m["name_cn"] for m in enabled] if state == "pending" else [],
        "disabled": [{"name": m["name_cn"], "note": (m["note"] or "")[:120]}
                     for m in mine if not m["enabled"]],
    }


def _build_card(m: dict, row: dict | None, d: str) -> dict:
    """单指标卡片。五要素 + (3) 要求的三样：as_of / sample_n / 距下次发布。"""
    code = m["code"]
    series = store.read_series(code, d, limit=_SPARK_N)
    card = {
        "code": code, "name": m["name_cn"], "unit": m["unit"], "freq": m["freq"],
        "direction": int(m["direction"]), "no_dist": int(m.get("no_dist") or 0),
        "weight": m["weight"],
        "spark": [{"d": s["trade_date"], "v": s["value"]} for s in series],
        "breaks": [b for b in (m["hist_break"] or "").split(",") if b],
        "break_note": _break_note(m, d),
    }
    if not series:                                # 从未有过任何行 = 数据源没接
        card.update(state="pending_source", note="数据源待接入")
        return card
    if row is None or row.get("value") is None:   # 有历史但当日无值 = 数据未更新
        last = next((s for s in reversed(series) if s["value"] is not None), None)
        card.update(state="missing", as_of=(row or {}).get("as_of") or (last or {}).get("as_of"),
                    note="数据未更新（不显示 0·不显示上一日值）")
        return card

    stale = int(row.get("is_stale") or 0)
    pctile = row.get("pctile_750")
    w = WINDOWS.get(m["freq"], WINDOWS["daily"])
    card.update(
        state="ok", value=row["value"], chg_1d=row.get("chg_1d"), chg_5d=row.get("chg_5d"),
        zscore=row.get("zscore_250"), pctile=pctile,
        adj_pctile=_adj(pctile, int(m["direction"])),
        sample_n=row.get("sample_n"), sample_win=w["pct_win"], sample_min=w["pct_min"],
        as_of=row.get("as_of"), stale_sessions=stale, anomaly=int(row.get("anomaly") or 0),
    )
    # (3) 距下次发布：月频/周频才有意义；相对**面板日期**算（回看模式下同样 point-in-time）
    if m["freq"] in ("monthly", "weekly") and row.get("as_of"):
        lag_days, nxt = publication_status(m["freq"], row["as_of"], int(m["lag_days"]), d)
        card["pub"] = {"stale_days": lag_days, "next_days": nxt}
    card["scored"], card["score_note"] = _score_state(m, row, d)
    return card


def _adj(pctile, direction: int):
    """"对A股是好是坏"的统一色标：利空指标翻转(100-p)；中性(direction=0)不给好坏色。"""
    if pctile is None or direction == 0:
        return None
    return round(100 - pctile, 1) if direction == -1 else round(pctile, 1)


def _score_state(m: dict, row: dict, d: str) -> tuple[bool, str]:
    """该指标当日是否参与评分 + 不参与的原因（与 compute.layer_scores 六条件一致）。"""
    if m.get("no_dist"):
        return False, "前瞻计划·无历史分布·不评分"
    if m["direction"] == 0:
        return False, "中性指标·只展示不评分"
    if m["weight"] <= 0:
        return False, "权重=0·已被手动停用评分"
    if m["score_from"] and d < m["score_from"]:
        return False, f"制度断点观察期·{m['score_from']} 起才计分"
    if row.get("pctile_750") is None:
        n = row.get("sample_n")
        need = WINDOWS.get(m["freq"], WINDOWS["daily"])["pct_min"]
        return False, f"分位样本不足（{n or 0}/{need}）·不计分"
    if m["freq"] == "daily" and int(row.get("is_stale") or 0) >= 2:
        return False, f"已沿用 {row['is_stale']} 个交易日·源降级·不计分"
    return True, ""


def _break_note(m: dict, d: str) -> str:
    """断点文字标注（用户定档：竖线给人看还不够·必须有文字）。"""
    breaks = [b for b in (m["hist_break"] or "").split(",") if b]
    if not breaks:
        return ""
    if m["break_mode"] == "truncate":
        return f"口径断点 {breaks[-1]}·分位仅用断点后数据"
    txt = f"窗口内含制度断点 {'、'.join(breaks)}"
    if m["score_from"] and d < m["score_from"]:
        txt += f"（{m['score_from']} 前只展示不计分）"
    return txt
