"""行情中枢：东财热榜 / 7×24快讯 / 财经日历 三源归一化（拉取层做轻量 TTL 缓存）。

akshare 东财列多为 pyarrow dtype → 一律 `to_dict("records")` 迭代解析（项目既有教训）。
盘面快变量（热榜/快讯）缓存短(3min)、财经日历慢变量缓存长(30min)，减轻 akshare 压力。
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

_CACHE: dict[str, tuple[float, list]] = {}


def _hot_disk(key: str) -> Path:
    d = get_settings().cache_dir / "market_hot"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def _fmt_hot(df, top: int, kind: str) -> list[dict]:
    rows = []
    for r in df.head(top).to_dict("records"):
        item = {"rank": r.get("当前排名"), "code": _clean(r.get("代码")),
                "name": _clean(r.get("股票名称")), "price": r.get("最新价"), "pct": r.get("涨跌幅")}
        if kind == "up":
            item["rank_chg"] = r.get("排名较昨日变动")
        rows.append(item)
    return rows


def hot_board(provider: CompositeProvider, kind: str = "rank", top: int = 40) -> dict:
    """东财热榜（kind=rank人气/up飙升）。快速失败：东财不可用时读上次磁盘缓存(标stale)。

    返回 {rows, as_of, stale}——东财对云IP偶发限流/不可用，故失败兜底上次成功结果。
    """
    ck = f"{kind}{top}"
    now = time.time()
    mem = _CACHE.get(ck)
    if mem and now - mem[0] < 60:               # 60秒内存缓存
        return mem[1]
    rows = []
    try:                                        # 仅一次(provider 内部已含重试)·不再死磕
        df = provider.get_hot_up() if kind == "up" else provider.get_hot_rank()
        if df is not None and not df.empty:
            rows = _fmt_hot(df, top, kind)
    except Exception:
        rows = []
    if rows:
        res = {"rows": rows, "as_of": datetime.datetime.now().strftime("%m-%d %H:%M:%S"), "stale": False}
        _CACHE[ck] = (now, res)
        try:
            _hot_disk(ck).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return res
    try:                                        # 东财挂了 → 读上次成功(磁盘)·标 stale
        d = json.loads(_hot_disk(ck).read_text(encoding="utf-8"))
        d["stale"] = True
        return d
    except Exception:
        return {"rows": [], "as_of": "", "stale": False}


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


def concept_heat(top: int = 30) -> list[dict]:
    """概念热度榜（用自家宽表·最可靠）→ [{name, heat, delta, money_flow_3d, pct_chg_3d, phase}]。"""
    def _f():
        from app.data.theme_heat_db import get_themes, latest_trade_date
        d = latest_trade_date("concept")
        if not d:
            return []
        rows = get_themes(d, "concept")[:top]          # 已按 heat_score 降序
        return [{
            "name": _clean(r.get("theme_name")),
            "heat": round(r["heat_score"], 1) if r.get("heat_score") is not None else None,
            "delta": round(r["heat_score_delta_3d"], 1) if r.get("heat_score_delta_3d") is not None else None,
            "money_flow_3d": r.get("money_flow_3d"),
            "pct_chg_3d": r.get("pct_chg_3d"),
            "phase": _clean(r.get("phase")),
            "date": d,
        } for r in rows]
    return _cached(f"concept{top}", 1800, _f)


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
