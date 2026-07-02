"""行情中枢：东财热榜 / 7×24快讯 / 财经日历 三源归一化（拉取层做轻量 TTL 缓存）。

akshare 东财列多为 pyarrow dtype → 一律 `to_dict("records")` 迭代解析（项目既有教训）。
盘面快变量（热榜/快讯）缓存短(3min)、财经日历慢变量缓存长(30min)，减轻 akshare 压力。
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from pathlib import Path

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)
_CACHE: dict[str, tuple[float, list]] = {}


def _hot_disk(key: str) -> Path:
    d = get_settings().cache_dir / "market_hot"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def save_hot_disk(kind: str, rows: list[dict], source: str = "本地同步") -> int:
    """把热榜写入磁盘（东财直连成功 or 本地电脑同步推送共用）。返回条数。

    本地同步用：家里电脑跑 local_hotrank_sync.py 拉东财→POST /api/market/hot/ingest→落这里。
    服务器读盘兜底(东财云上限流时)即用此数据，并标注同步时间/来源。
    """
    if not rows:
        return 0
    payload = {"rows": rows, "ts": time.time(),
               "as_of": datetime.datetime.now().strftime("%m-%d %H:%M:%S"), "source": source}
    _hot_disk(f"hot_{kind}").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:                                        # 顺带落每日快照·自建人气轨迹(供人气反转选股)·失败不影响主流程
        from app.strategy import db
        db.log_hot_rank(kind, rows)
    except Exception:
        logger.debug("[hot] 人气轨迹落库失败(不影响热榜)", exc_info=True)
    return len(rows)


def _load_hot_disk(kind: str) -> dict | None:
    """读盘热榜（本地同步/上次直连）。按同步时间算 stale(>30分钟视为过期)。"""
    try:
        d = json.loads(_hot_disk(f"hot_{kind}").read_text(encoding="utf-8"))
        d["stale"] = (time.time() - float(d.get("ts", 0))) > 1800
        return d
    except Exception:
        return None


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
    full = []
    try:                                        # 仅一次(provider 内部已含重试)·不再死磕
        df = provider.get_hot_up() if kind == "up" else provider.get_hot_rank()
        if df is not None and not df.empty:
            full = _fmt_hot(df, 100, kind)      # 存全量100(东财人气榜满榜)·读取再按top切
    except Exception:
        full = []
    if full:                                    # 东财直连成功 → 落盘 + 返回
        save_hot_disk(kind, full, "东财直连")
        res = {"rows": full[:top], "as_of": datetime.datetime.now().strftime("%m-%d %H:%M:%S"),
               "stale": False, "source": "东财直连"}
        _CACHE[ck] = (now, res)
        return res
    disk = _load_hot_disk(kind)                 # 东财挂了 → 读盘(本地同步/上次直连)
    if disk and disk.get("rows"):
        return {"rows": disk["rows"][:top], "as_of": disk.get("as_of", ""),
                "stale": disk.get("stale", True), "source": disk.get("source", "")}
    return {"rows": [], "as_of": "", "stale": False, "source": ""}


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


# 本地同步脚本（在用户家电脑跑·住宅IP能直连东财·推送到服务器兜底缓存）
LOCAL_SYNC_SCRIPT = r'''# local_hotrank_sync.py —— 在你【家里电脑】跑·拉东财人气/飙升榜→推送到服务器
# 东财对云服务器封IP·但你家住宅IP能直连。默认【循环模式】：运行后每5分钟自动同步一次
# (仅交易时段)，关窗口/关机就停——正好当备用。需先：pip install akshare requests
# 用法：python local_hotrank_sync.py   （Ctrl+C 停止）
import akshare as ak, requests, base64, time, datetime

SERVER = "http://123.207.223.176:8000"
AUTH = "Basic " + base64.b64encode(b"admin:Astock@2026").decode()
INTERVAL = 300        # 同步间隔(秒)·默认5分钟
LOOP = True           # True=开着就每5分钟自动同步 / False=只跑一次就退出

def fmt(df, kind):
    rows = []
    for _, r in df.head(100).iterrows():
        it = {"rank": int(r["当前排名"]), "code": str(r["代码"]),
              "name": str(r["股票名称"]), "price": float(r["最新价"]), "pct": float(r["涨跌幅"])}
        if kind == "up":
            it["rank_chg"] = int(r["排名较昨日变动"])
        rows.append(it)
    return rows

def push(kind, fn):
    rows = fmt(fn(), kind)
    requests.post(SERVER + "/api/market/hot/ingest", json={"kind": kind, "rows": rows},
                  headers={"Authorization": AUTH}, timeout=20)
    print(f"  {kind}: 已推送 {len(rows)} 条")

def is_market_hours():
    n = datetime.datetime.now()
    if n.weekday() >= 5:                         # 周末不跑
        return False
    hm = n.strftime("%H%M")
    return ("0925" <= hm <= "1135") or ("1300" <= hm <= "1505")

def sync_once():
    print(datetime.datetime.now().strftime("%H:%M:%S"), "同步中…")
    for kind, fn in [("rank", ak.stock_hot_rank_em), ("up", ak.stock_hot_up_em)]:
        try:
            push(kind, fn)
        except Exception as e:
            print(f"  {kind} 失败:", e)

if __name__ == "__main__":
    if not LOOP:
        sync_once()
    else:
        print("循环同步已启动 · 每%d秒一次(仅交易时段) · Ctrl+C 停止" % INTERVAL)
        while True:
            if is_market_hours():
                sync_once()
            else:
                print(datetime.datetime.now().strftime("%H:%M"), "非交易时段·跳过")
            time.sleep(INTERVAL)
# 想关掉窗口也继续跑(可选)：nohup python local_hotrank_sync.py >sync.log 2>&1 &
'''


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
