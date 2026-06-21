"""个股龙虎榜复盘：某只票在指定区间内的全部上榜记录 + 席位/资金风格 + 之后实际走势 + 规律。

数据约束：`top_inst` 只能按交易日查、不支持 ts_code+区间。逐日扫太慢，故用"异动日过滤"：
一只票只在异动日（涨跌≥7% / 振幅≥15%）才会上龙虎榜 → 先用它自己的日线找异动候选日
（6个月通常十几天），只对这几天查 top_inst。把上百次取数压到十几次。

每条上榜记录附 T+1/T+3/T+5 真实涨幅（从前复权日线算），并按资金风格分类聚合"规律"，
回答"这票机构买靠不靠谱、游资打板要不要躲"。诚实：历史规律≠未来，样本少仅参考。
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.kline_loader import load_kline
from app.strategy.lhb_seats import (_NET_SIG, infer_style, interpret_next_day,
                                     seat_rows)

_PCT_TRIG = 6.5      # |涨跌幅| 异动门槛（主板涨停10%/创业科创20%均覆盖）
_AMP_TRIG = 14.0     # 振幅 异动门槛
_MAX_SCAN = 45       # 候选异动日扫描上限（防极端·控取数次数）
_FWD = [1, 3, 5]     # 之后走势持有期


def _candidate_days(k: pd.DataFrame) -> list[str]:
    """从日线找可能上龙虎榜的异动候选日（升序）。"""
    dates = k["trade_date"].tolist()
    closes = k["close"].tolist(); highs = k["high"].tolist(); lows = k["low"].tolist()
    pcts = pd.to_numeric(k["pct_chg"], errors="coerce").tolist()
    out = []
    for i, d in enumerate(dates):
        pct = pcts[i] if pcts[i] == pcts[i] else 0.0
        prev = closes[i - 1] if i > 0 else closes[i]
        amp = (highs[i] - lows[i]) / prev * 100 if prev else 0.0
        if abs(pct) >= _PCT_TRIG or amp >= _AMP_TRIG:
            out.append(d)
    return out[-_MAX_SCAN:]


def _fwd_returns(k: pd.DataFrame, day: str, ns=_FWD) -> dict:
    """龙虎榜日之后 T+N 收盘相对当日收盘的涨幅（%）。未到期返回 None。"""
    dates = k["trade_date"].tolist(); closes = k["close"].tolist()
    if day not in dates:
        return {n: None for n in ns}
    i = dates.index(day); base = closes[i]
    return {n: (round((closes[i + n] / base - 1) * 100, 2) if i + n < len(closes) and base else None)
            for n in ns}


def _category(inst: float, hot: float, north: float) -> str:
    """按主导资金把一次上榜归类，用于规律聚合。"""
    if inst < -_NET_SIG:
        return "机构出货"
    if hot > _NET_SIG and hot >= inst:
        return "游资主导"
    if inst > _NET_SIG:
        return "机构净买"
    if north > _NET_SIG:
        return "北向加仓"
    if north < -_NET_SIG:
        return "北向流出"
    return "分歧/其他"


def _pattern(occ: list[dict]) -> list[dict]:
    """按资金风格分类聚合：各类出现次数 + 次日/T+5 平均涨幅 + T+5 胜率（纯函数）。"""
    g: dict[str, list] = defaultdict(list)
    for o in occ:
        g[o["category"]].append(o)
    rows = []
    for cat, items in g.items():
        t1 = [o["t1"] for o in items if o["t1"] is not None]
        t5 = [o["t5"] for o in items if o["t5"] is not None]
        rows.append({
            "category": cat, "count": len(items),
            "avg_t1": round(sum(t1) / len(t1), 2) if t1 else None,
            "avg_t5": round(sum(t5) / len(t5), 2) if t5 else None,
            "win_t5": round(sum(1 for x in t5 if x > 0) / len(t5) * 100) if t5 else None,
        })
    rows.sort(key=lambda r: -r["count"])
    return rows


def review_stock(provider: CompositeProvider, ts_code: str, start: str, end: str) -> dict:
    """个股龙虎榜复盘主入口。

    Returns:
        {ok, ts_code, start, end, count, occurrences:[...新→旧], pattern:[...]}
    """
    k = load_kline(ts_code, start, end, provider, adj="qfq")
    if k is None or k.empty:
        return {"ok": False, "msg": "该票区间内无行情数据"}

    occ = []
    for day in _candidate_days(k):
        try:
            df = provider.get_lhb_inst(day)
        except Exception:
            continue
        sub = df[df["ts_code"] == ts_code] if df is not None and not df.empty else None
        if sub is None or sub.empty:
            continue
        seats = seat_rows(sub)
        if not seats:
            continue
        inst = sum(s["net_yi"] for s in seats if s["type"] == "inst")
        hot = sum(s["net_yi"] for s in seats if s["type"] == "hot")
        north = sum(s["net_yi"] for s in seats if s["type"] == "north")
        reason = seats[0]["reason"]
        fwd = _fwd_returns(k, day)
        occ.append({
            "date": day, "reason": reason,
            "style_tags": infer_style(seats)["tags"],
            "next_day": interpret_next_day(seats, reason),
            "category": _category(inst, hot, north),
            "inst_net": round(inst, 2), "hot_net": round(hot, 2), "north_net": round(north, 2),
            "seat_count": len(seats),
            "t1": fwd[1], "t3": fwd[3], "t5": fwd[5],
        })
    occ.sort(key=lambda o: o["date"], reverse=True)
    return {"ok": True, "ts_code": ts_code, "start": start, "end": end,
            "count": len(occ), "occurrences": occ, "pattern": _pattern(occ)}


def _review_facts(review: dict, name: str) -> str:
    """把复盘结果拼成给 LLM 的事实块。"""
    lines = [f"个股：{name}（{review.get('ts_code')}）  区间：{review.get('start')}~{review.get('end')}",
             f"区间上龙虎榜 {review.get('count')} 次。"]
    lines.append("【按资金风格统计】")
    for p in review.get("pattern", []):
        lines.append(f"- {p['category']}：{p['count']}次，次日均{p['avg_t1']}%，T+5均{p['avg_t5']}%，T+5胜率{p['win_t5']}%")
    lines.append("【逐次明细（新→旧）】")
    for o in review.get("occurrences", []):
        tags = "/".join(t["text"] for t in o.get("style_tags", []))
        lines.append(f"- {o['date']} [{o['category']}] {tags} 机构净{o['inst_net']}亿·北向净{o['north_net']}亿·游资净{o['hot_net']}亿"
                     f" → 次日{o['t1']}% T+3 {o['t3']}% T+5 {o['t5']}%（{o['reason'][:16]}）")
    return "\n".join(lines)


def build_review_note(review: dict, name: str = "", client=None) -> dict:
    """LLM 读复盘规律 → 一段诚实解读（这票龙虎榜什么资金买/卖更靠谱、怎么用）。

    client 可注入便于单测；按指纹缓存避免重复花费。
    """
    import hashlib
    import json
    from pathlib import Path

    from app.config import get_settings
    from app.llm.stance import ANALYST_STANCE
    if not review.get("occurrences"):
        return {"ok": False, "msg": "该票区间内无上榜记录"}

    facts = _review_facts(review, name or review.get("ts_code", ""))
    cdir = get_settings().cache_dir / "lhb_review_note"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / (hashlib.md5(facts.encode("utf-8")).hexdigest()[:16] + ".json")
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    prompt = (ANALYST_STANCE + "\n\n"
              "下面是一只票在某区间的【龙虎榜复盘】：每次上榜的资金风格（机构/北向/游资谁在买卖）"
              "与之后 T+1/T+3/T+5 的真实涨幅，以及按风格的统计规律。请用 3-5 句话总结**这只票龙虎榜的规律**，"
              "帮投资者下次看到它上榜时怎么判断：\n"
              "- 哪类资金（机构/游资/北向）买入后历史表现更好、哪类要躲；\n"
              "- 样本少（某类<3次）要显式说'仅参考'；只引用给定数字，不编造；\n"
              "- 给可操作的一句话结论，但不打包票必涨、不预测具体点位。\n\n"
              f"数据：\n{facts}")
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    raw = client.chat([{"role": "user", "content": prompt}], task_type="pro", max_tokens=2000, temperature=0.3)

    st = get_settings()
    model = st.claude_model if st.llm_provider == "claude" else st.deepseek_pro_model
    out = {"ok": True, "note": (raw or "").strip(), "model": model,
           "disclaimer": "历史规律≠未来；龙虎榜含次日博弈与对倒，需结合位置/板块/基本面同看。"}
    if out["note"]:
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out
