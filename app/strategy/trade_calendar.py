"""交易日历判定（Tushare 交易日历·进程内按日缓存）。

用于：① 非交易日消息面报告的调度门禁；② 防止法定节假日撞工作日时常规报告空跑。
Tushare 不可用时优雅降级为"仅按周末判定"（无法识别法定节假日，但不至于崩）。
"""

from __future__ import annotations

import datetime as _dt
import logging

logger = logging.getLogger(__name__)

_CACHE: dict = {"key": "", "open": set()}
_FMT = "%Y%m%d"


def _today() -> str:
    return _dt.date.today().strftime(_FMT)


def _open_set() -> set[str]:
    """最近 ±15 天的开市日集合（进程内按日缓存）。失败返回空 → 调用方降级。"""
    key = _today()
    if _CACHE["key"] != key:
        today = _dt.date.today()
        start = (today - _dt.timedelta(days=15)).strftime(_FMT)
        end = (today + _dt.timedelta(days=15)).strftime(_FMT)
        try:
            from app.data.composite_provider import CompositeProvider
            cal = CompositeProvider().get_trade_cal(start, end)
            _CACHE["open"] = set(cal[cal["is_open"] == 1]["cal_date"].astype(str))
            _CACHE["key"] = key
        except Exception as e:
            logger.warning("[交易日历] 获取失败，降级按周末判定：%s", e)
            return set()
    return _CACHE["open"]


def is_trading_day(date: str | None = None) -> bool:
    """是否交易日。日历可用则精确（含节假日）；否则降级仅判周末。"""
    d = date or _today()
    opens = _open_set()
    if opens:
        return d in opens
    return _dt.datetime.strptime(d, _FMT).weekday() < 5      # 降级：周一~五视为交易日


def next_trading_day(date: str | None = None) -> str:
    """date 之后最近的交易日；查不到返回空串。"""
    d = _dt.datetime.strptime(date or _today(), _FMT).date()
    for i in range(1, 16):
        nd = (d + _dt.timedelta(days=i)).strftime(_FMT)
        if is_trading_day(nd):
            return nd
    return ""


def last_trading_day(date: str | None = None) -> str:
    """date 当日或之前最近的交易日（含当日）；查不到返回空串。"""
    d = _dt.datetime.strptime(date or _today(), _FMT).date()
    for i in range(0, 16):
        ld = (d - _dt.timedelta(days=i)).strftime(_FMT)
        if is_trading_day(ld):
            return ld
    return ""


def is_last_nontrading_before_open(date: str | None = None) -> bool:
    """今天是非交易日，且下一自然日是交易日（=周末/节假日的最后一晚）→ 适合推下周前瞻。"""
    d = date or _today()
    if is_trading_day(d):
        return False
    nd = (_dt.datetime.strptime(d, _FMT).date() + _dt.timedelta(days=1)).strftime(_FMT)
    return is_trading_day(nd)
