"""板块走势研究：申万二级行业指数 K线（日/周/月）+ 支撑压力带 + 阶段结构描述。

复用现有件（不重造轮子）：
  - sw_daily（申万行业指数日行情）→ 板块指数 OHLC；
  - stock_profile._kline_payload / _resample_ohlc → 日/周/月 payload（与个股走势同源）；
  - key_levels.build_key_levels → 支撑/压力带（对指数与个股用同一套算法）。

诚实纪律：全为盘后结构描述（近3年分位 / 均线排列 / 支撑压力），非预测、非买卖建议。
板块轮动快，用于"提前识别过热/低吸的板块位置"，仍需结合资金与个股。
"""

from __future__ import annotations

import datetime
import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.strategy.key_levels import build_key_levels
from app.strategy.stock_profile import _kline_payload, _resample_ohlc

logger = logging.getLogger(__name__)

_HISTORY_START = "20220101"    # 板块指数近 ~3.5 年（够周/月K + key_levels≥60日）
_DAILY_TAIL = 250
_WEEKLY_TAIL = 160
_MONTHLY_TAIL = 120


def build_sector_trend(name: str, kind: str = "industry",
                       provider: CompositeProvider | None = None) -> dict:
    """构建板块走势包：{ok, name, index_code, bars, kline/kline_w/kline_m, levels, stage, disclaimer}。"""
    provider = provider or CompositeProvider()
    code = _resolve_index_code(provider, name, kind)
    if not code:
        return {"ok": False, "name": name,
                "msg": f"未找到板块「{name}」的申万行业指数代码（暂仅支持申万二级行业）"}
    k = _load_index_kline(provider, code)
    if k is None or len(k) < 60:
        return {"ok": False, "name": name, "index_code": code,
                "msg": f"{name} 指数历史数据不足（需 ≥60 日）"}
    levels = build_key_levels(k)
    return {
        "ok": True, "name": name, "kind": kind, "index_code": code, "bars": len(k),
        "kline": _kline_payload(k.tail(_DAILY_TAIL)),
        "kline_w": _kline_payload(_resample_ohlc(k, "W-FRI").tail(_WEEKLY_TAIL)),
        "kline_m": _kline_payload(_resample_ohlc(k, "ME").tail(_MONTHLY_TAIL)),
        "levels": levels,
        "stage": _stage(k, levels),
        "disclaimer": ("申万行业指数·盘后结构描述：近3年分位/均线/支撑压力均非预测、非买卖建议。"
                       "板块轮动快，需结合资金与个股。"),
    }


def _resolve_index_code(provider: CompositeProvider, name: str, kind: str) -> str | None:
    """板块名 → 申万二级行业指数代码（index_classify L2 SW2021）。精确优先、退包含匹配。"""
    if kind != "industry":
        return None                                        # 概念指数(ths)后续扩展
    try:
        cl = provider._ts._api.index_classify(level="L2", src="SW2021")
        if cl is None or cl.empty:
            return None
        m = cl[cl["industry_name"] == name]
        if m.empty:
            m = cl[cl["industry_name"].str.contains(name, na=False, regex=False)]
        return str(m.iloc[0]["index_code"]) if not m.empty else None
    except Exception as e:
        logger.warning("[板块走势] 指数代码解析失败(%s): %s", name, e)
        return None


def _load_index_kline(provider: CompositeProvider, code: str) -> pd.DataFrame | None:
    """sw_daily → 对齐为日K DataFrame（trade_date/open/high/low/close/vol·升序）。"""
    end = datetime.date.today().strftime("%Y%m%d")
    try:
        df = provider._ts._api.sw_daily(ts_code=code, start_date=_HISTORY_START, end_date=end)
    except Exception as e:
        logger.warning("[板块走势] sw_daily 失败(%s): %s", code, e)
        return None
    if df is None or df.empty:
        return None
    keep = ["trade_date", "open", "high", "low", "close", "vol"]
    df = df[[c for c in keep if c in df.columns]].copy()
    for c in ("open", "high", "low", "close", "vol"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).sort_values("trade_date").reset_index(drop=True)


def _stage(k: pd.DataFrame, levels: dict | None) -> dict:
    """客观阶段描述：近3年区间分位 + 均线排列 + 最近支撑/压力带。"""
    close = k["close"].astype(float)
    px = float(close.iloc[-1])
    lo, hi = float(close.min()), float(close.max())
    pctile = round((px - lo) / (hi - lo) * 100) if hi > lo else 50
    ma20 = float(close.tail(20).mean()) if len(close) >= 20 else None
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else None
    arrange = "均线纠缠"
    if ma20 and ma60:
        if px > ma20 > ma60:
            arrange = "多头排列（价>MA20>MA60）"
        elif px < ma20 < ma60:
            arrange = "空头排列（价<MA20<MA60）"
        elif px >= ma20 and px >= ma60:
            arrange = "价在中短均线上方"
        elif px <= ma20 and px <= ma60:
            arrange = "价在中短均线下方"
    sup = (levels or {}).get("support") or []
    res = (levels or {}).get("resistance") or []
    return {
        "hist_pctile": pctile,                             # 近3年分位：0贴底 / 100贴顶
        "ma_arrange": arrange,
        "position": (levels or {}).get("position"),
        "nearest_support": sup[0] if sup else None,
        "nearest_resist": res[0] if res else None,
    }
