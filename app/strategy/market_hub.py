"""行情中枢：东财热榜 / 7×24快讯 / 财经日历 三源归一化（拉取层做轻量 TTL 缓存）。

akshare 东财列多为 pyarrow dtype → 一律 `to_dict("records")` 迭代解析（项目既有教训）。
盘面快变量（热榜/快讯）缓存短(3min)、财经日历慢变量缓存长(30min)，减轻 akshare 压力。
"""

from __future__ import annotations

import datetime
import time

from app.data.composite_provider import CompositeProvider

_CACHE: dict[str, tuple[float, list]] = {}


def _cached(key: str, ttl: int, fn) -> list:
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        data = fn()
    except Exception:
        data = []
    if data:                       # 拉到非空 → 更新缓存
        _CACHE[key] = (now, data)
        return data
    return hit[1] if hit else []   # 拉取失败/空 → 返回上次成功结果(即使过期)，避免空屏


def _clean(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.lower() == "nan" else s


def hot_rank(provider: CompositeProvider, top: int = 40) -> list[dict]:
    """东财人气榜 Top N → [{rank, code, name, price, pct}]。"""
    def _f():
        df = None
        for _ in range(3):                 # 东财人气榜对云IP偶发限流·多试几次
            try:
                df = provider.get_hot_rank()
                if df is not None and not df.empty:
                    break
            except Exception:
                df = None
                time.sleep(1.0)
        if df is None or df.empty:
            return []
        return [{
            "rank": r.get("当前排名"),
            "code": _clean(r.get("代码")),
            "name": _clean(r.get("股票名称")),
            "price": r.get("最新价"),
            "pct": r.get("涨跌幅"),
        } for r in df.head(top).to_dict("records")]
    return _cached(f"hot{top}", 300, _f)


def news_flash(provider: CompositeProvider, n: int = 50) -> list[dict]:
    """7×24 快讯（财联社电报·降级东财）→ [{time, title, summary, level, source, url}]。"""
    def _f():
        df = provider.get_cls_news(datetime.datetime.now().strftime("%Y%m%d"))
        if df is None or df.empty:
            return []
        return [{
            "time": _clean(r.get("发布时间"))[:19],
            "title": _clean(r.get("标题")),
            "summary": _clean(r.get("摘要")),
            "level": _clean(r.get("等级")),
            "source": _clean(r.get("来源")),
            "url": _clean(r.get("链接")),
        } for r in df.head(n).to_dict("records")]
    return _cached(f"news{n}", 180, _f)


def econ_calendar(provider: CompositeProvider, max_n: int = 80) -> list[dict]:
    """财经日历（经济数据/事件）→ [{date, time, region, event, actual, forecast, prev, importance}]。"""
    def _f():
        df = provider.get_econ_calendar()
        if df is None or df.empty:
            return []
        return [{
            "date": _clean(r.get("日期")),
            "time": _clean(r.get("时间")),
            "region": _clean(r.get("地区")),
            "event": _clean(r.get("事件")),
            "actual": _clean(r.get("公布")),
            "forecast": _clean(r.get("预期")),
            "prev": _clean(r.get("前值")),
            "importance": r.get("重要性"),
        } for r in df.head(max_n).to_dict("records")]
    return _cached("cal", 1800, _f)
