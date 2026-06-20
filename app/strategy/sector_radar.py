"""
板块广度雷达：在大盘情绪页提供「按板块/概念切换的广度时序图 + 今日低吸雷达」。

两块数据，分工清晰：
  1. 三栏诊断（低吸 / 轮动 / 高位风险）—— 复用 `sector_scope.build_sectorscope`（读宽表，
     纯因子已过滤）。**低吸候选已剔除"无资金/无催化/破位"的烂板块**，名单为空＝今日无
     值得低吸的板块（宁可不抄，符合"永远抄不到底的别推"）。
  2. 单板块广度时序图 —— **成分股实时计算**（复用 `sector_backtest.sector_breadth`）。
     因宽表广度历史仅按日累积（上线初期点数少），改用成分日K现算，立即给出完整曲线；
     取市值前 N 只代表性成分（控成本），按板块+日缓存，首次稍慢、之后秒回。

诚实纪律：缺数据显式标注、不补零、不造曲线；低吸候选只在有催化/资金/结构时给出。
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path

from app.backtest.sector_backtest import _market_cap_map, _ref_trade_date, sector_breadth
from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.data.kline_loader import load_kline

logger = logging.getLogger(__name__)

DEFAULT_MEMBERS = 50      # 广度取市值前 N 只代表性成分（控单进程成本）
DEFAULT_DAYS = 45         # 广度曲线展示交易日数（够看回踩位置）
_MAX_BOARDS = 80          # 下拉列表上限（按热度截断，避免 300+ 概念塞爆前端）


# ── 数值工具 ────────────────────────────────────────────────────────────────
def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fmt(v, unit: str = "", nd: int = 0, signed: bool = False) -> str:
    """安全格式化（None→'—'），带正负号与单位。"""
    n = _num(v)
    if n is None:
        return "—"
    s = f"{n:+.{nd}f}" if signed else f"{n:.{nd}f}"
    return f"{s}{unit}"


# ── 三栏催化理由（纯函数，便于单测）─────────────────────────────────────────
def _dip_reason(r: dict) -> str:
    return (f"5日资金净流入 {_fmt(r.get('money_flow_5d'), '亿', signed=True)}"
            f"·站上MA20 {_fmt(r.get('breadth_ma20'), '%')}(结构未破)"
            f"·今日 {_fmt(r.get('pct_chg_1d'), '%', 1, signed=True)}(回踩分歧) → 待回踩低吸")


def _rotate_reason(r: dict) -> str:
    return (f"主力3日净流入 {_fmt(r.get('money_flow_3d'), '亿', signed=True)}"
            f"·3日 {_fmt(r.get('pct_chg_3d'), '%', 1, signed=True)}"
            f"·站上MA20 {_fmt(r.get('breadth_ma20'), '%')} → 在途主线可追")


def _risk_reason(r: dict) -> str:
    return (f"5日涨幅 {_fmt(r.get('pct_chg_5d'), '%', 1, signed=True)} 居前"
            f"·站上MA20 {_fmt(r.get('breadth_ma20'), '%')}"
            f"·Top100占比 {_fmt(r.get('top100_ratio'), '%')} → 高位拥挤·防接盘")


def _bucket_row(r: dict, reason_fn) -> dict:
    """单个板块的诊断行（名 + 理由 + 关键数字，供前端点选载入广度图）。"""
    return {
        "name": r.get("theme_name", ""),
        "reason": reason_fn(r),
        "heat_score": _num(r.get("heat_score")),
        "breadth_ma20": _num(r.get("breadth_ma20")),
        "money_flow_5d": _num(r.get("money_flow_5d")),
        "pct_chg_1d": _num(r.get("pct_chg_1d")),
        "signals": r.get("signals", []),
    }


def _board_brief(r: dict) -> dict:
    """下拉列表用的精简板块条目。"""
    return {"name": r.get("theme_name", ""), "heat_score": _num(r.get("heat_score")),
            "phase": r.get("phase", ""), "breadth_ma20": _num(r.get("breadth_ma20")),
            "signal": r.get("signal", "")}


# ── 三栏雷达（读宽表·已过滤）─────────────────────────────────────────────────
def build_sector_radar(date: str = "", theme_type: str = "concept") -> dict:
    """
    构建板块雷达：板块下拉列表 + 三栏诊断（低吸/轮动/高位风险）。

    Returns:
        {ok, available, date, theme_type, boards[], dip[], rotate[], risk[], default, note}
        无宽表数据时 available=False（不展示旧/假数据）。
    """
    from app.strategy.sector_scope import build_sectorscope
    sc = build_sectorscope(date, theme_types=(theme_type,))
    if not sc.get("available"):
        return {"ok": True, "available": False, "date": sc.get("date", ""),
                "theme_type": theme_type, "msg": sc.get("msg", "宽表尚未计算")}

    rows = sc.get("rows", [])
    buckets = sc.get("buckets", {})
    dip = [_bucket_row(r, _dip_reason) for r in buckets.get("dip", [])]
    rotate = [_bucket_row(r, _rotate_reason) for r in buckets.get("rotate", [])]
    risk = [_bucket_row(r, _risk_reason) for r in buckets.get("risk", [])]
    boards = [_board_brief(r) for r in rows[:_MAX_BOARDS]]
    default = dip[0]["name"] if dip else (boards[0]["name"] if boards else "")

    return {
        "ok": True, "available": True, "date": sc.get("date", ""),
        "theme_type": theme_type, "boards": boards,
        "dip": dip, "rotate": rotate, "risk": risk, "default": default,
        "note": ("低吸候选＝中期资金/趋势仍在 + 结构未破 + 今日回踩分歧；"
                 "已过滤无资金/无催化/破位板块。名单为空＝今日无值得低吸的板块（宁可不抄）。"),
    }


# ── 单板块广度时序（成分股实时算·缓存）───────────────────────────────────────
def _cache_file(theme_type: str, name: str, end: str, days: int, max_n: int) -> Path:
    d = get_settings().cache_dir / "sector_radar"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w一-鿿]", "_", name)[:40]
    return d / f"{theme_type}_{safe}_{end}_{days}_{max_n}.json"


def get_board_members(provider: CompositeProvider, name: str, theme_type: str) -> list[str]:
    """解析板块成分 ts_code 列表。industry 走 stock_basic；concept 走同花顺成分（周缓存）。"""
    if theme_type == "industry":
        sb = provider.get_stock_basic()
        if sb is None or "industry" not in sb.columns:
            return []
        return sb[sb["industry"].astype(str) == name]["ts_code"].astype(str).tolist()
    from app.factors.theme_wide import concept_members_map
    return concept_members_map(provider).get(name, [])


def compute_board_breadth(name: str, theme_type: str = "concept", days: int = DEFAULT_DAYS,
                          provider: CompositeProvider | None = None,
                          max_members: int = DEFAULT_MEMBERS) -> dict:
    """
    单板块广度时序：成分中"% 站上 MA5 / MA20"随时间曲线（市值前 N 只代表性成分）。

    买卖无关，纯结构健康度。按 (板块, 日, 参数) 缓存，首次稍慢、之后秒回。
    """
    provider = provider or CompositeProvider()
    today = datetime.date.today().strftime("%Y%m%d")
    end = _ref_trade_date(provider, today)
    cache = _cache_file(theme_type, name, end, days, max_members)
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    members = _top_members(provider, name, theme_type, end, max_members)
    if not members:
        return {"ok": False, "name": name, "msg": f"未找到板块「{name}」成分"}

    series_map = _load_member_klines(provider, members, end, days)
    if not series_map:
        return {"ok": False, "name": name, "msg": "成分历史数据加载失败"}

    out = _breadth_to_payload(name, theme_type, end, len(series_map),
                              sector_breadth(series_map), days)
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out


def _top_members(provider: CompositeProvider, name: str, theme_type: str,
                 end: str, max_members: int) -> list[str]:
    """取板块成分按流通市值前 N（代表性 + 控成本）。"""
    members = get_board_members(provider, name, theme_type)
    if not members:
        return []
    mv = _market_cap_map(provider, end)
    return sorted(members, key=lambda c: mv.get(c, 0.0), reverse=True)[:max_members]


def _load_member_klines(provider: CompositeProvider, members: list[str],
                        end: str, days: int) -> dict:
    """加载成分前复权日K（含 MA20 预热缓冲），失败的成分跳过。"""
    buf_start = (datetime.datetime.strptime(end, "%Y%m%d")
                 - datetime.timedelta(days=int((days + 30) * 1.6))).strftime("%Y%m%d")
    series_map: dict = {}
    for code in members:
        try:
            k = load_kline(code, buf_start, end, provider, adj="qfq")
            if k is not None and not k.empty:
                series_map[code] = k
        except Exception:
            logger.debug("[radar] 成分 %s 加载失败，跳过", code)
    return series_map


def _breadth_to_payload(name: str, theme_type: str, end: str, n_members: int,
                        breadth, days: int) -> dict:
    """sector_breadth 结果 → 前端曲线 payload（裁到最近 days 个有效点）。"""
    import pandas as pd
    df = breadth.dropna(how="all").tail(days)
    curve = [{"date": str(idx),
              "ma5": None if pd.isna(r["pct_ma5"]) else float(r["pct_ma5"]),
              "ma20": None if pd.isna(r["pct_ma20"]) else float(r["pct_ma20"])}
             for idx, r in df.iterrows()]
    current = curve[-1] if curve else {"ma5": None, "ma20": None}
    return {"ok": True, "name": name, "theme_type": theme_type, "end": end,
            "n_members": n_members, "curve": curve, "current": current}
