"""
申万板块 point-in-time 成分重建（真·时点·杜绝成分漂移的幸存者偏差）。

用 index_member 历史（in_date/out_date·含已调出成分）重建任意历史日 t 的成分：
    t 日在册 = { in_date ≤ t 且 (out_date 为空 或 out_date > t) }
同时消除"未来新增股"（回测早期不该含后来才调入的票）与"已调出股"（历史某日在册但现已剔除）两类偏差。

⚠️ 残余偏差：Tushare 成分库**不含已退市股** → 退市幸存者偏差（**有界·且仅往后逐步收敛的残差**）。
   已退市的 327 只**无法补回**；每日成分快照落库只能捕获"从今天起往后"退市的票，
   故偏差随时间往后收敛、并非"长期消除"。上层须按"有界且往后收敛的残差"如实标注。

粒度：L1(31) / L2(134→剔7兜底=127) / L3(346)。兜底/无主题一致性行业剔除，避免污染状态机与回测。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

# 兜底/无主题一致性的申万二级（污染状态机·剔除）
JUNK_L2 = {"农业综合Ⅱ", "其他电子Ⅱ", "摩托车及其他", "其他家电Ⅱ",
           "其他银行Ⅱ", "综合Ⅱ", "其他电源设备Ⅱ"}

_LEVEL_COL = {"L1": "l1_name", "L2": "l2_name", "L3": "l3_name"}


def load_history(provider: CompositeProvider | None = None) -> pd.DataFrame:
    """申万成分历史（缓存·含已调出）。列：ts_code/l1_name/l2_name/l3_name/in_date/out_date/is_new。空→空表。"""
    provider = provider or CompositeProvider()
    h = provider.get_sw_member_history()
    if h is None or h.empty:
        return pd.DataFrame()
    h = h.copy()
    for c in ("in_date", "out_date"):
        if c in h.columns:
            h[c] = h[c].astype("string")
    return h


def members_asof(hist: pd.DataFrame, date: str, level: str = "L2",
                 exclude_junk: bool = True) -> dict[str, list[str]]:
    """时点成分 {板块名: [ts_code...]}：date 当日在册（in_date≤date 且 out_date空或>date）。

    level: L1/L2/L3。exclude_junk: L2 剔除兜底行业。空/缺列→{}。
    """
    col = _LEVEL_COL.get(level)
    if hist is None or hist.empty or col not in hist.columns:
        return {}
    d = str(date)
    ind, outd = hist["in_date"], hist["out_date"]
    active = (ind <= d) & (outd.isna() | (outd > d))            # 时点在册判定
    sub = hist[active]
    out: dict[str, list[str]] = {}
    for name, g in sub.groupby(col):
        nm = str(name)
        if not nm or nm == "<NA>":
            continue
        if exclude_junk and level == "L2" and nm in JUNK_L2:
            continue
        codes = g["ts_code"].dropna().unique().tolist()
        if codes:
            out[nm] = codes
    return out


def asof_map(date: str, level: str = "L2", provider: CompositeProvider | None = None,
             exclude_junk: bool = True) -> dict[str, list[str]]:
    """便捷：加载历史 + 重建 date 时点成分。"""
    return members_asof(load_history(provider), date, level, exclude_junk)


def clean_sectors(hist: pd.DataFrame, level: str = "L2") -> list[str]:
    """某级别所有（剔兜底后）板块名列表（供导航/遍历）。"""
    col = _LEVEL_COL.get(level)
    if hist is None or hist.empty or col not in hist.columns:
        return []
    names = [str(n) for n in hist[col].dropna().unique()]
    if level == "L2":
        names = [n for n in names if n not in JUNK_L2]
    return sorted(n for n in names if n and n != "<NA>")
