"""
板块广度预算（提速）：盘后 `wide` 任务复用已加载的全市场前复权面板，一次性算好
全部板块（申万二级行业 + 同花顺概念）的「% 站上 MA5 / MA20」时序并缓存。

动机：前端切板块原来要现场冷加载 ~50 只成分日K（Tushare 限频·实测首切 ~74 秒）。
改为盘后预算 → 切板块直接读缓存（秒开），且用全部成分（非市值前N采样）更准、给满历史。

口径与 `sector_backtest.sector_breadth` 一致（`收盘 > MAx`），保证与实时回退结果一致。
缺缓存（如 wide 未跑或新板块）→ 端点回退实时计算，不影响可用性。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from app.config import get_settings

logger = logging.getLogger(__name__)

_KEEP_DAYS = 90          # 缓存保留交易日数（前端按需 tail，默认展示 45）
_MIN_MEMBERS = 3         # 成分过少不算（统计不可靠）


# ── 缓存路径 ────────────────────────────────────────────────────────────────
def _cache_dir() -> Path:
    d = get_settings().cache_dir / "board_breadth"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(theme_type: str, name: str) -> str:
    return f"{theme_type}::{name}"


# ── 纯函数：从全市场面板算单板块广度时序 ─────────────────────────────────────
def breadth_series_from_panel(panel: pd.DataFrame, codes: list[str],
                              keep: int = _KEEP_DAYS) -> list[dict]:
    """
    panel: build_qfq_panel 输出（index=ts_code，columns=交易日升序，值=前复权收盘）。
    返回 [{date, ma5, ma20}...]（升序，裁到最近 keep 个有效点）。成分过少返回 []。
    """
    if panel is None or panel.empty:
        return []
    valid = [c for c in codes if c in panel.index]
    if len(valid) < _MIN_MEMBERS:
        return []
    mat = panel.reindex(valid).T.apply(pd.to_numeric, errors="coerce")   # rows=日期, cols=成分
    ma5, ma20 = mat.rolling(5).mean(), mat.rolling(20).mean()
    b5 = (mat > ma5).where(ma5.notna()).mean(axis=1) * 100
    b20 = (mat > ma20).where(ma20.notna()).mean(axis=1) * 100
    df = pd.DataFrame({"ma5": b5.round(1), "ma20": b20.round(1)}).dropna(how="all").tail(keep)
    return [{"date": str(idx),
             "ma5": None if pd.isna(r["ma5"]) else float(r["ma5"]),
             "ma20": None if pd.isna(r["ma20"]) else float(r["ma20"])}
            for idx, r in df.iterrows()]


# ── 预算并落缓存（盘后 wide 调用）─────────────────────────────────────────────
def precompute_board_breadth(trade_date: str, provider=None) -> int:
    """
    对全部行业(申万二级)+概念(同花顺去噪)板块预算广度时序并缓存到
    data_cache/board_breadth/{trade_date}.json。返回成功板块数。
    """
    from app.data.composite_provider import CompositeProvider
    from app.factors.breadth_qfq import build_qfq_panel
    from app.factors.theme_wide import concept_members_map, concept_members_map_wide

    provider = provider or CompositeProvider()
    panel = build_qfq_panel(trade_date, provider, lookback=145)
    if panel is None or panel.empty:
        logger.warning("[板块广度预算] %s 面板为空，跳过", trade_date)
        return 0

    out: dict[str, dict] = {}
    _fill(out, "industry", _industry_members(provider), panel)
    _fill(out, "concept", concept_members_map(provider), panel)
    # 补大概念(>300成分·窄口径丢的热点大主题·如人形机器人)·面板已含全码·仅多几次向量化
    try:
        _fill(out, "concept", concept_members_map_wide(provider), panel)
    except Exception as e:
        logger.warning("[板块广度预算] 大概念补充失败: %s", e)
    _cache_dir().joinpath(f"{trade_date}.json").write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    logger.info("[板块广度预算] %s 写入 %d 个板块", trade_date, len(out))
    return len(out)


def _industry_members(provider) -> dict[str, list[str]]:
    """{申万二级行业名: [ts_code]}（口径同宽表，industry 已覆盖为申万二级）。"""
    sb = provider.get_stock_basic()
    if sb is None or "industry" not in sb.columns:
        return {}
    return {str(ind): g["ts_code"].tolist()
            for ind, g in sb.dropna(subset=["industry"]).groupby("industry")}


def _fill(out: dict, theme_type: str, members: dict[str, list[str]],
          panel: pd.DataFrame) -> None:
    for name, codes in members.items():
        curve = breadth_series_from_panel(panel, codes)
        if curve:
            n = len([c for c in codes if c in panel.index])
            out[_key(theme_type, name)] = {"n_members": n, "curve": curve}


# ── 读缓存（端点优先调用）────────────────────────────────────────────────────
def load_cached_breadth(theme_type: str, name: str, days: int = 45) -> dict | None:
    """读最近一份预算缓存（前端 payload）。无缓存/无此板块 → None（端点回退实时算）。"""
    files = sorted(_cache_dir().glob("*.json"))
    if not files:
        return None
    latest = files[-1]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return None
    item = data.get(_key(theme_type, name))
    if not item or not item.get("curve"):
        return None
    curve = item["curve"][-days:]
    return {"ok": True, "name": name, "theme_type": theme_type, "end": latest.stem,
            "n_members": item.get("n_members"), "curve": curve,
            "current": curve[-1] if curve else {"ma5": None, "ma20": None},
            "cached": True}
