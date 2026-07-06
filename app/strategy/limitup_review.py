"""每日涨停复盘：板块涨停梯队 + 连板梯队情绪 + 涨停股全景(含涨停原因) + 龙虎榜游资/机构动向。

盘后 EOD(settled)·JSON日缓存·供 /limitup 页秒开。数据源(Tushare)：
- 涨停名单+连板+首封+开板+行业：`limit_list_d`(get_limit_list 'U')
- 涨停原因/题材/封板时间：`kpl_list`(开盘啦)
- 龙虎榜营业部级：`top_inst`(get_lhb_inst·exalter/net_buy/reason) + `hm_list`(游资名录·识别游资)

诚实：涨停原因来自开盘啦官方·游资名按官方名录 hm_list 识别(非臆测点名)·机构=龙虎榜专用席真钱。**非买卖建议**·仅复盘描述。
"""

from __future__ import annotations

import json
import logging

import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

_TIER_NAMES = {5: "五板核心", 4: "四板核心", 3: "三板核心", 2: "二板核心", 1: "一板核心"}


def _ff(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt_time(v) -> str:
    """封板时间 → HH:MM。原始形如 '92503'/'092503'/'131820'/'09:25:03'(缺前导零/带冒号皆容错)。"""
    s = str(v or "").strip().replace(":", "").split(".")[0]
    if not s.isdigit() or s in ("", "0"):
        return ""
    s = s.zfill(6)
    return f"{s[:2]}:{s[2:4]}"


# ── 涨停股记录：limit_list_d + kpl_list 合并 ──────────────────────────────────
def _zt_records(date: str, prov: CompositeProvider) -> list[dict]:
    """当日收盘涨停股(limit=='U') → 每只 {code,name,industry,limit_times,first_time,open_times,amount_yi,theme,reason}。"""
    ll = prov.get_limit_list(date, "U")
    if ll is None or ll.empty:
        return []
    try:
        kpl = prov.get_kpl_list(date)
    except Exception:
        kpl = None
    theme_map, reason_map = {}, {}
    if kpl is not None and not kpl.empty:
        for _, r in kpl.iterrows():
            theme_map[str(r["ts_code"])] = str(r.get("theme") or "")
            reason_map[str(r["ts_code"])] = str(r.get("lu_desc") or "")
    out = []
    for _, r in ll.iterrows():
        ts = str(r["ts_code"])
        out.append({
            "code": ts, "name": str(r.get("name", "")), "industry": str(r.get("industry") or "其他"),
            "limit_times": int(_ff(r.get("limit_times")) or 1),
            "up_stat": str(r.get("up_stat") or ""),
            "first_time": _fmt_time(r.get("first_time")),
            "open_times": int(_ff(r.get("open_times"))),
            "amount_yi": round(_ff(r.get("amount")) / 1e8, 2),
            "pct_chg": round(_ff(r.get("pct_chg")), 2),
            "theme": theme_map.get(ts, ""), "reason": reason_map.get(ts, ""),
        })
    return out


# ── ① 板块涨停梯队 ───────────────────────────────────────────────────────────
def _sector_ladder(recs: list[dict], top: int = 12) -> list[dict]:
    """按行业聚合涨停数·每板块龙一/二/三(连板高→成交额)·按涨停数排。"""
    from collections import defaultdict
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        buckets[r["industry"]].append(r)
    out = []
    for ind, items in buckets.items():
        ranked = sorted(items, key=lambda x: (x["limit_times"], x["amount_yi"]), reverse=True)
        dragons = [{"name": s["name"], "code": s["code"], "lb": s["limit_times"]} for s in ranked[:3]]
        out.append({"sector": ind, "count": len(items), "dragons": dragons,
                    "leader": ranked[0]["name"] if ranked else ""})
    out.sort(key=lambda x: x["count"], reverse=True)
    return out[:top]


# ── ② 连板梯队 ───────────────────────────────────────────────────────────────
def _lianban_ladder(recs: list[dict]) -> list[dict]:
    """连板梯队：五/四/三/二/一板核心(每档取成交额前3)。"""
    from collections import defaultdict
    by_lb: dict[int, list[dict]] = defaultdict(list)
    for r in recs:
        by_lb[min(r["limit_times"], 5)].append(r)     # 5板及以上并档
    out = []
    for lb in (5, 4, 3, 2, 1):
        items = sorted(by_lb.get(lb, []), key=lambda x: x["amount_yi"], reverse=True)
        if not items:
            continue
        out.append({"tier": _TIER_NAMES[lb], "lb": lb, "n": len(items),
                    "core": [{"name": s["name"], "code": s["code"], "amount_yi": s["amount_yi"]}
                             for s in items[:3]]})
    return out


# ── ③ 情绪指标（破板率 + 昨强今弱表现）───────────────────────────────────────
def _sentiment(date: str, recs: list[dict], prov: CompositeProvider) -> dict:
    """今日破板率 + 昨日涨停/连板/破板 今表现 + 大幅回撤家数(赚钱/亏钱效应)。"""
    out: dict = {"zt_count": len(recs), "lianban_count": sum(1 for r in recs if r["limit_times"] >= 2)}
    # 破板率(炸板率) = 炸板 / (涨停 + 炸板)。炸板走 limit_list_d 的 'Z' 专用查询(kpl tag 不含"炸板"字样)
    try:
        z = prov.get_limit_list(date, "Z")
        zhaban = len(z) if z is not None and not z.empty else 0
        out["zhaban"] = zhaban
        out["break_rate"] = round(zhaban / (len(recs) + zhaban) * 100, 1) if (len(recs) + zhaban) else 0.0
    except Exception:
        pass
    # 昨日强势今表现：需上一交易日涨停/连板名单 + 今日涨跌
    try:
        from app.nodes.quick_report import _recent_trade_dates
        ds = _recent_trade_dates(prov, date, 2)
        prev = ds[-2] if (len(ds) >= 2 and ds[-1] == date) else None
        if prev:
            today = prov.get_daily(date)
            pct = dict(zip(today["ts_code"].astype(str),
                           pd.to_numeric(today["pct_chg"], errors="coerce"))) if today is not None else {}
            y_ll = prov.get_limit_list(prev, "U")
            if y_ll is not None and not y_ll.empty:
                y = y_ll.copy()
                y["lt"] = pd.to_numeric(y["limit_times"], errors="coerce")
                zt_codes = y["ts_code"].astype(str).tolist()
                lb_codes = y[y["lt"] >= 2]["ts_code"].astype(str).tolist()
                zt_p = [pct[c] for c in zt_codes if c in pct and pd.notna(pct[c])]
                lb_p = [pct[c] for c in lb_codes if c in pct and pd.notna(pct[c])]
                if zt_p:
                    out["y_zt_perf"] = round(sum(zt_p) / len(zt_p), 2)
                    out["big_retrace"] = int(sum(1 for x in zt_p if x <= -3))    # 昨涨停今跌>3%=大幅回撤
                if lb_p:
                    out["y_lb_perf"] = round(sum(lb_p) / len(lb_p), 2)
    except Exception as e:
        logger.debug("[涨停复盘] 情绪(昨表现)失败: %s", e)
    return out


# ── ④ 龙虎榜游资 / 机构动向 ──────────────────────────────────────────────────
def _hm_map(prov: CompositeProvider) -> dict:
    """{营业部全名: 游资名}（Tushare hm_list 官方游资名录·周缓存）。"""
    from app.data.cache import cached_daily
    import datetime as _dt
    iso = _dt.date.today().isocalendar()

    def _fetch():
        hm = prov._ts._api.hm_list()
        return hm if hm is not None else pd.DataFrame()
    hm = cached_daily("hm_list", f"{iso[0]}W{iso[1]:02d}", _fetch)
    m: dict = {}
    if hm is None or hm.empty:
        return m
    for _, r in hm.iterrows():
        name = str(r["name"])
        try:
            orgs = json.loads(r["orgs"]) if isinstance(r.get("orgs"), str) else (r.get("orgs") or [])
        except Exception:
            orgs = []
        for org in orgs:
            m[str(org)] = name
    return m


def _lhb_flows(date: str, prov: CompositeProvider, name_map: dict) -> dict:
    """龙虎榜三路真钱：知名游资(hm_list识别) + 机构专用席 + 北向(股通专用)·均按个股聚合净额。

    北向/机构专用席的名称在 hm_list 里也被登记为"游资"，故先按 exalter 前缀分流(股通专用/机构专用)，
    剩下才用 hm_list 匹配真游资，避免北向/机构污染游资榜。同一游资在一票的多个营业部/多条上榜原因合并。
    """
    li = prov.get_lhb_inst(date)
    if li is None or li.empty:
        return {"hot": [], "inst": [], "north": []}
    # 同一标的上多个龙虎榜(多上榜原因)时同一席位会被重复列出·仅 reason/*_rate(相对各榜单占比)不同·
    # 按绝对金额列去重避免净额双计(同席位同股同日买卖额是唯一物理量)
    _dedup = [c for c in ("ts_code", "exalter", "buy", "sell", "net_buy") if c in li.columns]
    li = li.drop_duplicates(subset=_dedup)
    hm = _hm_map(prov)
    hot_agg: dict = {}      # (游资名, ts) -> net元
    inst_by: dict = {}      # ts -> net元(机构专用席)
    north_by: dict = {}     # ts -> net元(北向·股通专用)
    reason_by: dict = {}
    for _, r in li.iterrows():
        ts = str(r["ts_code"])
        exalter = str(r.get("exalter") or "")
        net = _ff(r.get("net_buy"))
        if "机构专用" in exalter:
            inst_by[ts] = inst_by.get(ts, 0.0) + net
        elif "股通专用" in exalter:                     # 深/沪股通专用=北向·非游资
            north_by[ts] = north_by.get(ts, 0.0) + net
        else:
            hm_name = hm.get(exalter)
            if hm_name:                                 # 官方名录识别为知名游资
                key = (hm_name, ts)
                hot_agg[key] = hot_agg.get(key, 0.0) + net
                reason_by.setdefault(ts, str(r.get("reason") or "")[:12])

    def _yi_list(by: dict, n: int) -> list[dict]:
        rows = [{"name": name_map.get(ts, ts[:6]), "code": ts, "net_yi": round(v / 1e8, 2)}
                for ts, v in by.items()]
        rows.sort(key=lambda x: abs(x["net_yi"]), reverse=True)
        return rows[:n]

    hot = [{"hm": k[0], "name": name_map.get(k[1], k[1][:6]), "code": k[1],
            "net_wan": round(v / 1e4, 0), "reason": reason_by.get(k[1], "")}
           for k, v in hot_agg.items()]
    hot.sort(key=lambda x: abs(x["net_wan"]), reverse=True)
    return {"hot": hot[:15], "inst": _yi_list(inst_by, 10), "north": _yi_list(north_by, 8)}


# ── 主构建（盘后·日缓存）─────────────────────────────────────────────────────
def build_limitup_review(date: str, force: bool = False,
                         provider: CompositeProvider | None = None) -> dict:
    """当日涨停复盘全景(4块)·JSON日缓存·数据入库后(约17:40)才落缓存。"""
    cdir = get_settings().cache_dir / "limitup_review"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{date}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    prov = provider or CompositeProvider()
    recs = _zt_records(date, prov)
    if not recs:
        return {"ok": False, "date": date, "msg": "当日无涨停数据(或盘后未入库·约17:40后)"}

    sb = prov.get_stock_basic()
    name_map = dict(zip(sb["ts_code"].astype(str), sb["name"].astype(str))) if sb is not None else {}
    # 开盘啦(题材/涨停原因)settle 比涨停名单晚·当晚可能未入库→标记待更新·且不落缓存(让后续请求自愈)
    kpl_ready = any(r.get("theme") or r.get("reason") for r in recs)
    out = {
        "ok": True, "date": date, "reason_pending": not kpl_ready,
        "sectors": _sector_ladder(recs),
        "ladder": _lianban_ladder(recs),
        "sentiment": _sentiment(date, recs, prov),
        "stocks": sorted(recs, key=lambda x: (x["limit_times"], x["amount_yi"]), reverse=True),
        "lhb": _lhb_flows(date, prov, name_map),
        "note": ("涨停原因/题材=开盘啦(kpl_list)官方；连板/首封/开板=limit_list_d；"
                 "游资名=Tushare官方名录hm_list识别；机构=龙虎榜专用席真钱·非估算。盘后EOD·非买卖建议。"),
    }
    if kpl_ready:                                       # 仅当题材就绪才落缓存·避免缓存住空白原因
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out
