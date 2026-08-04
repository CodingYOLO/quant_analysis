"""派生量计算：chg / zscore / 分位(point-in-time) / 异动 / 分层评分。

夜间任务全量重算（943天×16指标 <2s）后落库，幂等；回补与增量共用。
所有滚动统计**右端=当前行**，结构上无法用到未来数据——回看模式的分位与当日真实可得一致。

四条已定的设计（2026-08-02 用户定档）：
① 统计只在**新值序列**（is_stale=0）上算，沿用日继承最近新值的统计——
   月频若在 forward-fill 后的日频序列上算，同一值重复 20 次会把分布压扁、分位失真；
② `hist_break` 与 `score_from` 是两个维度：mark 断点**分位照常全窗口算并正常显示**，
   只在评分环节由 score_from 把门；truncate 断点才截断统计窗口（语义断点·两段不是同一个量）；
③ 分层评分分母动态：指标被跳过（stale/未到 score_from/无分位）时其权重同步从 Σweight 剔除，
   否则得分被系统性拉低；n_part/n_total 落库供 UI 显示"本层 8 项中 6 项参与评分"；
④ direction 翻转在**分位**上做：利空指标 adj = 100 − pctile（× -1 会产生负分、量纲不一致、加总失效）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.macro import store

logger = logging.getLogger(__name__)

# 统计窗口（按发布频率缩放：月频的"750个交易日"≈36次发布、"250"≈12次）。
# 月频 zscore 用 36 而非 12：12 个点的 std 噪声太大，宁可窗口长一点也别给出抖动的 z。
WINDOWS: dict[str, dict[str, int]] = {
    "daily":   {"pct_win": 750, "pct_min": 250, "z_win": 250, "z_min": 250},
    "weekly":  {"pct_win": 156, "pct_min": 52,  "z_win": 52,  "z_min": 52},
    "monthly": {"pct_win": 36,  "pct_min": 12,  "z_win": 36,  "z_min": 12},
}

# 异动判定：raw = |z|>z_th **或** |chg|>近一年95分位(point-in-time·shift1)；
# 日频再加确认：**连续 2 个新值日同向触发才报**（日频金融序列尾部厚·滤单日噪音）；
# 月频/周频 = 发布事件·单次即报（下一发布在一个月后，无"连续"可言）。
#
# 档位由 2024-01 起 624 个交易日 × 15 指标的真实触发频率研究选定（目标 0-2条/天）：
#   V0 |z|>2 OR  p95           日均0.96  最多6/日  >2条66天   ← 需求原文·右尾太肥
#   V1 |z|>2.5 OR p95          日均0.51  最多6/日  >2条34天
#   V2 两条件AND               日均0.06  35个信号日            ← 过静·亚转折全漏
#   V3 OR + 连续2日同向确认 ⭐  日均0.40  最多3/日  >2条仅5天  ← 采用
#   V4 AND + 确认              624天仅1次                     ← 模块等于废了
# V3 的 5 个多条日全是真实宏观转折(20240220=LPR5Y大降息日·20240906=美联储9月降息前
# 美债急跌·20241227=年末资金面收紧)——相关指标同日共振·多条是特征不是噪音。
ANOMALY: dict[str, object] = {
    "z_th": 2.0,
    "combine": "or",
    "chg_q": 0.95,
    "chg_win": 250,            # 日频：近一年新值日
    "chg_min": 100,
    "confirm_daily": True,     # 日频需连续2新值日同向（0.96→0.40·砍掉六成单日噪音）
}
# TODO(2026-09 复检·用户定档)：V3 是在 2024-01~2026-07 的波动率环境下调的档。
# 上线跑满一个月后回看实际触发频率：若掉到每周不到 1 条·说明当前环境下偏紧·z_th 2.0→1.8。
# 复看方法：SELECT trade_date,COUNT(*) FROM macro_daily WHERE anomaly!=0 GROUP BY 1 近30日。


@dataclass
class ComputeResult:
    end: str
    codes: int
    rows_updated: int
    score_days: int
    anomalies_today: list[dict] = field(default_factory=list)
    layer_today: list[dict] = field(default_factory=list)


# ──────────────────────────────────────────────
# 滚动统计（纯函数·可单测）
# ──────────────────────────────────────────────

def rolling_stats(fresh: pd.Series, freq: str, breaks: list[str],
                  break_mode: str) -> pd.DataFrame:
    """新值序列 → pctile/zscore/sample_n（point-in-time·右端=当前行）。

    truncate：窗口在断点处截断（各段独立·绝不跨段），sample_n 随之重置——北向那种
    语义断点，断点前后不是同一个量，混窗算出的分位是错的。
    mark：全窗口照算（制度断点·可比性未破坏），评分环节的门禁由 score_from 负责（见模块头②）。
    """
    if fresh.empty:
        return pd.DataFrame(columns=["pctile", "zscore", "sample_n"])
    if break_mode == "truncate" and breaks:
        seg = pd.Series(0, index=fresh.index)
        for b in sorted(breaks):
            seg[fresh.index >= b] += 1          # 索引为 YYYYMMDD 字符串·字典序即时间序
        parts = [_stats_one(g, freq) for _, g in fresh.groupby(seg)]
        return pd.concat(parts).sort_index()
    return _stats_one(fresh, freq)


def _stats_one(s: pd.Series, freq: str) -> pd.DataFrame:
    w = WINDOWS.get(freq, WINDOWS["daily"])
    rp = s.rolling(w["pct_win"], min_periods=w["pct_min"])
    pct = rp.rank(pct=True) * 100               # 当前值在窗口内的分位·右端=自身·无前视
    # sample_n 单独用 min_periods=1：它的意义就是让"样本 120/250·分位留空"可见，
    # 若跟随 pct 的 min_periods 会在最需要它解释的时候恰好是 NaN
    n = s.rolling(w["pct_win"], min_periods=1).count()
    rz = s.rolling(w["z_win"], min_periods=w["z_min"])
    std = rz.std()
    z = (s - rz.mean()) / std.where(std > 0)    # 常数段 std=0 → z 留空(不给 inf)
    return pd.DataFrame({"pctile": pct, "zscore": z, "sample_n": n})


def changes(fresh: pd.Series, unit: str, freq: str) -> tuple[pd.Series, pd.Series]:
    """1期/5期变动。利率类(unit ∈ %/bp)用**绝对差**(百分点)，价格类用涨跌幅%。

    月频/周频：chg_1d = 相邻两次**发布**之间的变化（发布日才有值·沿用日留空——
    "日变动"对月频的沿用日没有意义，填 0 会稀释异动阈值的分布）；chg_5d 无意义留空。
    """
    # 绝对差 vs 涨跌幅：利率(%/bp)按百分点差；**量级类(亿元/亿份/家)也必须绝对差**——
    # 资金净流入可正可负·pct_change 分母过零会算出 -517% 这类无意义数(924回看实测抓到)。
    # 无单位比值(汇率)/点位/**商品价格(单位形如 元/吨·恒正不过零)**用涨跌幅。
    absolute = unit not in ("", "点") and not unit.startswith(("元/", "美元/"))
    c1 = fresh.diff() if absolute else fresh.pct_change() * 100
    c5 = fresh.diff(5) if absolute else fresh.pct_change(5) * 100
    c1 = c1.replace([np.inf, -np.inf], np.nan)
    c5 = c5.replace([np.inf, -np.inf], np.nan)
    if freq != "daily":
        c5 = pd.Series(np.nan, index=fresh.index)
    return c1, c5


def anomaly_flags(chg1: pd.Series, z: pd.Series, freq: str,
                  cfg: dict | None = None) -> pd.Series:
    """异动标记：0=无 · +1=向上异动 · -1=向下异动（只在新值日打标）。

    阈值 point-in-time：|chg| 的 95 分位在**过去一年的新值日**上滚动计算并 shift(1)，
    绝不含当日自己（含自己会把大变动日的阈值抬高、恰好漏掉它自己）。
    """
    cfg = cfg or ANOMALY
    if chg1.empty:
        return pd.Series(dtype=int)
    th = (chg1.abs().rolling(int(cfg["chg_win"]), min_periods=int(cfg["chg_min"]))
          .quantile(float(cfg["chg_q"])).shift(1))
    hit_c = chg1.abs() > th
    hit_z = z.abs() > float(cfg["z_th"])
    raw = (hit_z & hit_c) if cfg["combine"] == "and" else (hit_z | hit_c)
    direction = np.sign(chg1).where(chg1.notna(), np.sign(z))
    if freq == "daily" and cfg.get("confirm_daily"):
        confirmed = raw & raw.shift(1, fill_value=False) & (direction == direction.shift(1))
    else:
        confirmed = raw                          # 月频=发布事件·单次即报
    return (confirmed.astype(int) * direction).fillna(0).astype(int)


def publication_status(freq: str, as_of: str, lag_days: int,
                       today: str) -> tuple[int, int | None]:
    """卡片标注用：(数据已滞后天数, 距下次发布约N天·daily 返回 None)。

    用户定档(2026-08-02)：月频/周频沿用日照常计分后，必须让"数据时点 X·距下次发布 N 天"
    可见——否则 L0 分数横盘时分不清是环境没变还是月频指标本来就不会动。
    下次发布估算：月频=下月同期(报告期月末+1月+lag)，周频=+7天+lag；粗估即可(±2天)，
    事件日历里的精确发布日由 macro_calendar 负责。
    """
    t, a = pd.Timestamp(today), pd.Timestamp(as_of)
    stale_days = max(0, (t - a).days - lag_days)
    if freq == "monthly":
        nxt = a + pd.DateOffset(months=1) + pd.Timedelta(days=lag_days)
    elif freq == "weekly":
        nxt = a + pd.Timedelta(days=7 + lag_days)
    else:
        return stale_days, None
    return stale_days, max(0, (nxt - t).days)


# ──────────────────────────────────────────────
# 分层评分（纯函数·可单测）
# ──────────────────────────────────────────────

def layer_scores(panel: pd.DataFrame, meta: dict[str, dict]) -> pd.DataFrame:
    """逐日分层评分。panel 列：trade_date/code/value/pctile/is_stale。

    参与条件（六条·缺一即从分子**和分母**同时剔除，见模块头③）：
      value 非空 · pctile 非空 · direction≠0 · weight>0 ·
      trade_date ≥ score_from · (daily 频率 → is_stale ≤ 1：外盘隔夜差1个会话属正常，
      ≥2 会话=源降级·只展示不计分；weekly/monthly 的沿用是其固有节奏·照常计分)
    """
    df = panel.copy()
    for col, key, default in (("direction", "direction", 0), ("weight", "weight", 0.0),
                              ("layer", "layer", ""), ("freq", "freq", "daily"),
                              ("score_from", "score_from", "")):
        df[col] = df["code"].map(lambda c, k=key, d=default: meta.get(c, {}).get(k, d))

    ok = (df["value"].notna() & df["pctile"].notna()
          & (df["direction"] != 0) & (df["weight"] > 0)
          & ((df["score_from"] == "") | (df["trade_date"] >= df["score_from"]))
          & ((df["freq"] != "daily") | (df["is_stale"] <= 1)))
    df["adj"] = np.where(df["direction"] == 1, df["pctile"], 100 - df["pctile"])

    part = df[ok]
    grp = part.groupby(["trade_date", "layer"])
    agg = pd.DataFrame({
        "score": grp.apply(lambda g: float(np.average(g["adj"], weights=g["weight"])),
                           include_groups=False),
        "n_part": grp.size(),
    }).reset_index()

    # 分母全集：该层"原则上可评分"的启用指标数（direction≠0 且 weight>0）
    n_total = {}
    for c, m in meta.items():
        if m.get("direction", 0) != 0 and m.get("weight", 0) > 0:
            n_total[m["layer"]] = n_total.get(m["layer"], 0) + 1

    all_days = sorted(df["trade_date"].unique())
    rows = []
    have = {(r.trade_date, r.layer): r for r in agg.itertuples()}
    for d in all_days:
        day_scores = []
        for layer in store.LAYERS:
            r = have.get((d, layer))
            score = round(float(r.score), 2) if r else None
            rows.append({"trade_date": d, "layer": layer, "score": score,
                         "n_part": int(r.n_part) if r else 0,
                         "n_total": int(n_total.get(layer, 0))})
            if score is not None:
                day_scores.append(score)
        rows.append({"trade_date": d, "layer": "TOTAL",
                     "score": round(float(np.mean(day_scores)), 2) if day_scores else None,
                     "n_part": len(day_scores), "n_total": len(store.LAYERS)})
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────
# 全量重算入口
# ──────────────────────────────────────────────

def run(end_date: str | None = None, run_id: str = "") -> ComputeResult:
    """对全部启用指标重算派生量并落库；随后重算全历史分层评分。"""
    store.init_db()
    metas = store.get_meta()
    meta_by = {m["code"]: m for m in metas}
    end = end_date or store.latest_date()
    updates: list[dict] = []
    panel_parts: list[pd.DataFrame] = []

    for m in metas:
        rows = store.read_series(m["code"], end)
        if not rows:
            continue
        df = pd.DataFrame(rows).set_index("trade_date")
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        fresh = df[(df["is_stale"] == 0) & df["value"].notna()]["value"]
        breaks = [b for b in (m["hist_break"] or "").split(",") if b]

        if int(m.get("no_dist") or 0):
            # 前瞻/计划类(如未来4周解禁)：未来值没有历史分布可比——分位/z/chg/异动全部留空，
            # 只保留 value 供展示(2026-08-02 定档 (c))
            empty = pd.Series(dtype=float)
            stats = pd.DataFrame(index=fresh.index,
                                 columns=["pctile", "zscore", "sample_n"], dtype=float)
            c1, c5, anom = empty, empty, pd.Series(dtype=int)
        else:
            stats = rolling_stats(fresh, m["freq"], breaks, m["break_mode"])
            c1, c5 = changes(fresh, m["unit"] or "", m["freq"])
            anom = anomaly_flags(c1, stats.get("zscore", pd.Series(dtype=float)), m["freq"])

        # 沿用日继承最近新值的统计；chg/anomaly 只属于新值日本身，不继承
        full = stats.reindex(df.index).ffill()
        full.loc[df["value"].isna(), ["pctile", "zscore", "sample_n"]] = np.nan
        full["chg_1d"], full["chg_5d"] = c1.reindex(df.index), c5.reindex(df.index)
        full["anomaly"] = anom.reindex(df.index).fillna(0).astype(int)

        for d, r in full.iterrows():
            updates.append({
                "trade_date": d, "code": m["code"],
                "chg_1d": _f(r["chg_1d"]), "chg_5d": _f(r["chg_5d"]),
                "zscore_250": _f(r["zscore"]), "pctile_750": _f(r["pctile"]),
                "sample_n": int(r["sample_n"]) if pd.notna(r["sample_n"]) else None,
                "anomaly": int(r["anomaly"]),
            })
        panel_parts.append(pd.DataFrame({
            "trade_date": df.index, "code": m["code"], "value": df["value"].values,
            "pctile": full["pctile"].values, "is_stale": df["is_stale"].values,
        }))

    store.update_derived(updates)
    scores = layer_scores(pd.concat(panel_parts, ignore_index=True), meta_by) \
        if panel_parts else pd.DataFrame()
    if not scores.empty:
        _check_bounds(scores)
        store.upsert_scores([dict(r, run_id=run_id) for r in scores.to_dict("records")])

    return ComputeResult(
        end=end, codes=len(metas), rows_updated=len(updates),
        score_days=int(scores["trade_date"].nunique()) if not scores.empty else 0,
        anomalies_today=_today_anomalies(updates, meta_by, end),
        layer_today=[r for r in scores.to_dict("records") if r["trade_date"] == end]
        if not scores.empty else [],
    )


def _f(v) -> float | None:
    return round(float(v), 4) if pd.notna(v) else None


def _check_bounds(scores: pd.DataFrame) -> None:
    """评分必须恒在 [0,100]——加权平均的数学性质保证，越界=实现有 bug，立刻抛。"""
    s = scores["score"].dropna()
    if not s.empty and (s.min() < 0 or s.max() > 100):
        raise AssertionError(f"分层评分越界 [{s.min()}, {s.max()}]，direction 翻转实现有误")


def _today_anomalies(updates: list[dict], meta: dict, end: str) -> list[dict]:
    out = []
    for u in updates:
        if u["trade_date"] == end and u["anomaly"]:
            m = meta.get(u["code"], {})
            out.append({"code": u["code"], "name_cn": m.get("name_cn", u["code"]),
                        "direction": u["anomaly"], "chg_1d": u["chg_1d"],
                        "zscore": u["zscore_250"], "pctile": u["pctile_750"]})
    return out
