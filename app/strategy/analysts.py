"""
🏅 金牌分析师榜（东财·免费）：按分析师历史荐股收益率排名 + 最新荐股 + 跟踪记录下钻。

数据（走 provider 纪律）：
  - stock_analyst_rank_em：排名/收益率(年度/3·6·12月)/成分股数/最新荐股/行业/分析师ID。
  - stock_analyst_detail_em(历史跟踪成分股)：某分析师的跟踪记录（调入/调出/调入时评级）。

诚实红线：收益率为东财统计的历史业绩，**过往业绩不代表未来**；不构成投资建议。
"""

from __future__ import annotations

import datetime as _dt
import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

# 科技赛道相关的分析师覆盖行业（东财行业口径；贴用户主投：半导体/光/算力/通信/计算机）
_TECH_INDUSTRIES = ("电子", "通信", "计算机", "半导体", "电力设备", "机械设备")
_DISCLAIMER = "金牌分析师榜=东财按分析师历史荐股收益率排名；过往业绩不代表未来，不构成投资建议。"


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _col(df: pd.DataFrame, *keys: str) -> str | None:
    """按子串找列名（容忍东财列名带年份前缀，如 2025最新个股评级-股票名称）。"""
    for k in keys:
        for c in df.columns:
            if k in c:
                return c
    return None


def get_analyst_board(year: str = "", tech_only: bool = False, top: int = 60,
                      provider: CompositeProvider | None = None) -> dict:
    """金牌分析师榜：收益率排名 + 最新荐股 + 行业。tech_only 仅留科技覆盖行业。"""
    provider = provider or CompositeProvider()
    y = year or str(_dt.date.today().year)
    try:
        df = provider.get_analyst_rank(y)
    except Exception as e:
        return {"ok": False, "msg": f"分析师排名获取失败：{str(e)[:50]}"}
    if df is None or df.empty:
        return {"ok": False, "msg": "暂无分析师排名数据"}
    rows = _board_rows(df, tech_only, top)
    return {"ok": bool(rows), "year": y, "tech_only": tech_only, "count": len(rows),
            "analysts": rows, "disclaimer": _DISCLAIMER,
            "msg": "" if rows else "该筛选下暂无分析师（可切回全部）"}


def _board_rows(df: pd.DataFrame, tech_only: bool, top: int) -> list[dict]:
    """排名表 → 前端行（用 records 迭代，避开 pyarrow 列 .apply 兼容问题）。纯函数，便于单测。"""
    yr_col = _col(df, "年收益率")           # 如「2025年收益率」
    name_col = _col(df, "最新个股评级-股票名称")
    code_col = _col(df, "最新个股评级-股票代码")
    out: list[dict] = []
    for r in df.to_dict("records"):         # 已按收益率降序；先过滤再取 top
        ind = str(r.get("行业", "") or "")
        if tech_only and not any(k in ind for k in _TECH_INDUSTRIES):
            continue
        out.append({
            "rank": int(_num(r.get("序号")) or 0),
            "name": str(r.get("分析师名称", "") or ""),
            "org": str(r.get("分析师单位", "") or ""),
            "industry": ind,
            "ret_year": _num(r.get(yr_col)) if yr_col else None,
            "ret_3m": _num(r.get("3个月收益率")),
            "ret_6m": _num(r.get("6个月收益率")),
            "ret_12m": _num(r.get("12个月收益率")),
            "n_stocks": int(_num(r.get("成分股个数")) or 0),
            "pick": str(r.get(name_col, "") or "") if name_col else "",
            "pick_code": str(r.get(code_col, "") or "") if code_col else "",
            "analyst_id": str(r.get("分析师ID", "") or ""),
        })
        if len(out) >= top:
            break
    return out


def get_analyst_picks(analyst_id: str, provider: CompositeProvider | None = None,
                      top: int = 25) -> dict:
    """某分析师跟踪记录（历史跟踪成分股）：当前持有(调出为空)优先 + 调入日期/评级。"""
    if not analyst_id:
        return {"ok": False, "msg": "缺少 analyst_id"}
    provider = provider or CompositeProvider()
    try:
        df = provider.get_analyst_detail(analyst_id, "历史跟踪成分股")
    except Exception as e:
        return {"ok": False, "msg": f"分析师明细获取失败：{str(e)[:50]}"}
    if df is None or df.empty:
        return {"ok": False, "msg": "暂无跟踪记录"}
    return _picks(df, top)


_EMPTY_OUT = ("", "-", "nan", "None", "NaT")


def _picks(df: pd.DataFrame, top: int) -> dict:
    """历史跟踪明细 → 前端（当前持有优先）。用 records 迭代避开 pyarrow 兼容问题。纯函数。"""
    items = []
    for r in df.to_dict("records"):
        out_raw = str(r.get("调出日期", "") or "").strip()
        held = out_raw in _EMPTY_OUT
        items.append({
            "code": str(r.get("股票代码", "") or ""), "name": str(r.get("股票名称", "") or ""),
            "in_date": str(r.get("调入日期", "") or ""),
            "out_date": "" if held else out_raw,
            "rating": str(r.get("调入时评级名称", "") or ""), "held": held,
        })
    items.sort(key=lambda x: (x["held"], x["in_date"]), reverse=True)  # 当前持有优先,再按调入日期新→旧
    held_n = sum(1 for i in items if i["held"])
    return {"ok": True, "n": len(items), "held": held_n,
            "items": items[:top], "disclaimer": _DISCLAIMER}
