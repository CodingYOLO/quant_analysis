"""沪深两市大盘主力净流入（超大单+大单口径）——经 Tushare `moneyflow_dc`（东财资金流官方转发）。

为何用它而非东财直连：东财 push2 对服务器 IP 反爬（实测闷断十几分钟），不可作生产依赖；
`moneyflow_dc` 是东财同一份数据的 Tushare 转发，**可靠**（付费·无反爬）、**口径一致**
（实测 2026-07-09 聚合 +226亿 vs 东财直连 +224.6亿 吻合）、**带历史**。

口径：主力净流入 = 东财"主力"（超大单+大单净额），与推文/东财 APP 的"大盘净流入"同口径。
范围：沪深两市（剔北交所 .BJ）对齐东财沪深口径。
时效：盘后结算（当日 dc 约 21:00 才齐）→ 盘中显示最近已结算日 + 近 N 日序列。

缓存：已结算日聚合值不可变 → 内存 + 磁盘缓存（避免重复聚合每天≈5900行）；
整体结果再套 120s TTL，限制盘中对 Tushare 的调用频次。
"""

from __future__ import annotations

import datetime
import json
import logging
import time

import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

_RESULT_TTL = 120.0                       # 整体结果缓存（秒）·限盘中调用频次
_mem: dict[str, float] = {}               # {trade_date: 主力净流入(亿)}·已结算日不可变
_disk_loaded = [False]
_result_cache: dict = {"ts": 0.0, "days": 0, "data": None}


def get_market_main_flow(provider: CompositeProvider | None = None, days: int = 6) -> dict:
    """近 days 个交易日沪深大盘主力净流入（亿）。

    返回 {ok, today:{date, net_yi}, series:[{date, net_yi}...]}（series 升序·最早→最新）。
    today = 序列最后一个已有数据日（盘中通常为上一交易日，当日 dc 结算后才含今日）。
    """
    now = time.time()
    if _result_cache["data"] and _result_cache["days"] == days and now - _result_cache["ts"] < _RESULT_TTL:
        return _result_cache["data"]
    data = _build(provider or CompositeProvider(), days)
    _result_cache.update(ts=now, days=days, data=data)
    return data


def _build(prov: CompositeProvider, days: int) -> dict:
    """逐交易日取聚合值（命中缓存跳过·仅当日/未缓存日重算）→ 组装序列。"""
    from app.nodes.quick_report import _recent_trade_dates
    _load_disk()
    today = datetime.date.today().strftime("%Y%m%d")
    dates = _recent_trade_dates(prov, today, days + 2) or []
    series: list[dict] = []
    dirty = False
    for d in dates:
        cached = _mem.get(d)
        v = cached
        if cached is None or d >= today:                  # 未缓存 或 当日(可能刚结算)→重算
            fresh = _aggregate_day(prov, d)
            if fresh is not None:
                v = fresh
                if _mem.get(d) != fresh:
                    _mem[d] = fresh
                    if d < today:                         # 只把已结算日落盘(当日易变·不落)
                        dirty = True
        if v is not None:
            series.append({"date": d, "net_yi": v})
    if dirty:
        _save_disk()
    series = series[-days:]
    return {"ok": bool(series), "today": series[-1] if series else None, "series": series}


def _aggregate_day(prov: CompositeProvider, date: str) -> float | None:
    """某交易日沪深主力净流入（亿）= Σ moneyflow_dc.net_amount(万元·剔.BJ) / 1e4。无数据→None。"""
    try:
        dc = prov._ts._api.moneyflow_dc(trade_date=date)
    except Exception as e:
        logger.debug("[大盘资金] moneyflow_dc(%s) 拉取失败: %s", date, e)
        return None
    if dc is None or dc.empty or "net_amount" not in dc.columns:
        return None
    net = pd.to_numeric(dc["net_amount"], errors="coerce")
    if "ts_code" in dc.columns:                            # 沪深口径·剔北交所(对齐东财沪深两市)
        net = net[~dc["ts_code"].astype(str).str.endswith(".BJ")]
    return round(float(net.sum()) / 1e4, 1)                # 万元 → 亿


# ── 磁盘缓存（已结算日聚合值·跨重启复用）─────────────────────────────────────
def _cache_file():
    d = get_settings().cache_dir / "market_fund"
    d.mkdir(parents=True, exist_ok=True)
    return d / "main_flow_yi.json"


def _load_disk() -> None:
    if _disk_loaded[0]:
        return
    _disk_loaded[0] = True
    try:
        p = _cache_file()
        if p.exists():
            _mem.update({k: float(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()})
    except Exception as e:
        logger.debug("[大盘资金] 读盘缓存失败: %s", e)


def _save_disk() -> None:
    try:
        _cache_file().write_text(json.dumps(_mem, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("[大盘资金] 写盘缓存失败: %s", e)
