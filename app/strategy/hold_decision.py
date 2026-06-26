"""拿得住 · 卖出决策器。

把《A股持有手册》的「卖不卖」四问，对一只真实持仓做**数据接地**的判定：
能客观判断的（破位没/放量缩量/趋势锚/距止损）用真实数据自动答，
只把"买入逻辑还在吗"留给用户自查。

手册铁律（务必守住）：
- **卖出判定只看趋势 / 纪律位 / 量价，绝不看成本价**（治锚定成本：市场不知道你买在哪）。
- 盈亏只用于"仓位是否过重"的自检，**不进入卖出判定**。
- 不预测顶底、不打包票涨跌；用客观条件替代临场情绪。

输入是 portfolio._build_row 产出的持仓行（含 price/above_ma20/ma20_up/volume_ratio/
stop_loss/main_flow_3d/events/note/pnl 等）。纯函数，零网络，便于单测。
"""

from __future__ import annotations

_NEAR_STOP_PCT = 3.0          # 距止损位 ≤3% 视为"贴近"
_VOL_HEAVY = 1.5              # 量比 ≥1.5 = 放量
_VOL_LIGHT = 0.8             # 量比 ≤0.8 = 缩量

# 判定等级 → 展示用（灯色/排序）
LEVEL_ORDER = {"sell": 0, "warn": 1, "watch": 2, "hold_soft": 3, "hold": 4}


def _check_logic(row: dict) -> dict:
    """Q1 买入逻辑（主观·留给用户自查）：带出当初买入理由，提醒'逻辑破了无条件清仓'。"""
    note = (row.get("note") or "").strip()
    if note:
        detail = f"当初买入理由：{note}。问自己——这个理由现在还成立吗？"
    else:
        detail = "你没记录买入理由（手册：填不出买入理由，就是没想清楚就买了）。先想清楚你为什么持有它。"
    return {"q": "① 买入逻辑还在吗？", "state": "ask", "detail": detail,
            "note": "逻辑若已被证伪 → 无条件清仓，无论此刻赚亏。"}


def _check_stop(row: dict) -> dict:
    """Q2 止损 / 关键支撑：现价 vs 止损位（手册：止损是执行不是商量）。"""
    price, stop = row.get("price"), row.get("stop_loss")
    if not stop:
        return {"q": "② 跌破止损位了吗？", "state": "warn",
                "detail": "你没设止损位——这本身是隐患（手册：卖出条件要在买入那刻就写死）。建议立刻补一个关键支撑/均线/固定百分比。"}
    if price is None:
        return {"q": "② 跌破止损位了吗？", "state": "ask", "detail": f"止损位 {stop}（暂无现价，无法比对）。"}
    if price <= stop:
        return {"q": "② 跌破止损位了吗？", "state": "bad",
                "detail": f"已跌破止损位 {stop}（现价 {price}）。按纪律执行，别在这里讲'再等等'的故事。"}
    gap = (price / stop - 1) * 100
    if gap <= _NEAR_STOP_PCT:
        return {"q": "② 跌破止损位了吗？", "state": "warn",
                "detail": f"贴近止损位：现价 {price} 距止损 {stop} 仅 {gap:.1f}%，跌破即按计划走。"}
    return {"q": "② 跌破止损位了吗？", "state": "ok",
            "detail": f"未破止损（现价 {price} / 止损 {stop}，缓冲 {gap:.1f}%）。"}


def _check_trend_volume(row: dict) -> dict:
    """Q3 趋势锚 + 量价性质：站上MA20=趋势未破；破位看放量(资金撤)还是缩量(洗盘)。"""
    above20 = row.get("above_ma20")
    above60 = row.get("above_ma60")
    up = row.get("ma20_up")
    vr = row.get("volume_ratio")
    ma20 = row.get("ma20")
    vtxt = f"（量比 {vr}）" if vr is not None else ""
    if above20:                                    # 趋势锚未破 → 持有依据
        if up:
            return {"q": "③ 趋势锚 / 量价", "state": "ok",
                    "detail": f"站上20日线（生命线 {ma20}）且MA20向上——趋势健康，日内怎么晃都是噪音。"}
        return {"q": "③ 趋势锚 / 量价", "state": "ok",
                "detail": f"站在20日线上方（{ma20}），但MA20走平——趋势在但动能转弱，留意是否走坏。"}
    # 跌破 MA20：量能是测谎仪
    if vr is not None and vr >= _VOL_HEAVY:
        return {"q": "③ 趋势锚 / 量价", "state": "bad",
                "detail": f"放量跌破20日线{vtxt}——通常是资金在撤、危险破位，给趋势一点尊重。"}
    if vr is not None and vr <= _VOL_LIGHT:
        tail = "且守住60日线" if above60 else "但已跌破60日线"
        return {"q": "③ 趋势锚 / 量价", "state": "warn",
                "detail": f"缩量跌破20日线{vtxt}{tail}——多是洗盘而非出货，别恐慌卖，看能否快速站回。"}
    return {"q": "③ 趋势锚 / 量价", "state": "warn",
            "detail": f"跌破20日线{vtxt}，量能中性——收紧止盈、观察能否站回生命线。"}


def _check_position(row: dict) -> dict:
    """Q4 仓位（只自检·不进卖出判定）：带出浮盈亏，提醒'慌往往是仓位太重'。"""
    pnl = row.get("pnl")
    ptxt = (f"当前浮盈亏 {pnl:+.1f}%。" if pnl is not None else "")
    return {"q": "④ 仓位会不会太重？", "state": "ask",
            "detail": f"{ptxt}问自己：它明天跌停，我睡得着吗？睡不着就减到睡得着——这是治本。",
            "note": "这条只用来判断仓位，不影响上面的卖出判定（卖出只看趋势纪律，不看你赚没赚）。"}


def _last_valid(arr):
    for x in reversed(arr or []):
        if x is not None:
            return x
    return None


def decide_for_code(ts_code: str, name: str = "", provider=None) -> dict:
    """对**任意股票**（不必是持仓）现取数据走4问——处理"临时冲动想动手"时的冷静判断。

    结构信号(站MA20/MA20向上/量比/趋势)从日K算(看日线不看分时·手册原则)，现价用实时；
    若恰好在你持仓里，自动带出成本/止损/买入理由。返回 {ok, ...decide()}。
    """
    if provider is None:
        from app.data.composite_provider import CompositeProvider
        provider = CompositeProvider()
    from app.strategy.stock_profile import build_stock_profile
    prof = build_stock_profile(ts_code, name, provider)
    if not prof.get("ok"):
        return {"ok": False, "msg": prof.get("msg", "数据不足，无法判断")}

    kl = prof.get("kline") or {}
    closes = [c[1] for c in (kl.get("candle") or []) if c]
    ma20_arr, ma60_arr, vols = kl.get("ma20") or [], kl.get("ma60") or [], kl.get("vol") or []
    ma20, ma60 = _last_valid(ma20_arr), _last_valid(ma60_arr)

    price = pct = None                                       # 现价：实时优先，回退到日K收盘
    try:
        q = provider.get_realtime_quote([ts_code])
        if q is not None and not q.empty:
            price = round(float(q.iloc[0]["price"]), 2)
            pct = round(float(q.iloc[0]["pct_chg"]), 2)
    except Exception:
        pass
    if price is None and closes:
        price = round(float(closes[-1]), 2)

    above20 = (price >= ma20) if (price and ma20) else None
    above60 = (price >= ma60) if (price and ma60) else None
    ma20_up = (ma20_arr[-1] > ma20_arr[-4]) if (len(ma20_arr) >= 4 and ma20_arr[-1] and ma20_arr[-4]) else None
    vr = None
    if len(vols) >= 6 and vols[-1] is not None:
        prior = [v for v in vols[-6:-1] if v]
        if prior:
            vr = round(vols[-1] / (sum(prior) / len(prior)), 2)

    cost = stop = note = pnl = None                         # 若恰好是持仓 → 带出成本/止损/理由
    try:
        from app.strategy import db
        for w in (db.get_watchlist() or []):
            if w.get("ts_code") == ts_code:
                cost, stop, note = w.get("cost"), w.get("stop_loss"), w.get("note")
                break
        if cost and price:
            pnl = round((price / cost - 1) * 100, 2)
    except Exception:
        pass

    row = {"ts_code": ts_code, "name": name or prof.get("name") or ts_code,
           "price": price, "pct_chg": pct, "above_ma20": above20, "above_ma60": above60,
           "ma20_up": ma20_up, "volume_ratio": vr, "ma20": round(ma20, 2) if ma20 else None,
           "stop_loss": stop, "pnl": pnl, "note": note}
    return {"ok": True, "name": row["name"], "ts_code": ts_code, "price": price, "pct_chg": pct,
            "is_holding": bool(cost or stop), **decide(row)}


def decide(row: dict) -> dict:
    """对一只持仓给出'卖不卖'的纪律化判定 + 四问逐条（数据接地）。

    Returns:
        {verdict, level, why, checks:[4], pnl, anchor_note}
        level ∈ sell / warn / watch / hold_soft / hold（见 LEVEL_ORDER）。
    """
    checks = [_check_logic(row), _check_stop(row),
              _check_trend_volume(row), _check_position(row)]
    price, stop = row.get("price"), row.get("stop_loss")
    above20, above60 = row.get("above_ma20"), row.get("above_ma60")
    up, vr = row.get("ma20_up"), row.get("volume_ratio")

    # 纪律优先级：① 破止损 → ② 放量破位 → ③ 缩量回踩(洗盘) → ④ 中性破位 → ⑤ 站上MA20持有
    if stop and price is not None and price <= stop:
        verdict, level = "按纪律止损 / 减仓", "sell"
        why = "已跌破你买入前设好的止损位——止损是用来执行的，不是用来突破的。"
    elif above20 is False and vr is not None and vr >= _VOL_HEAVY:
        verdict, level = "警惕 · 分批减", "warn"
        why = "放量跌穿20日线，多是资金在撤；分批减、收紧移动止盈，让仓位轻下来再看。"
    elif above20 is False and vr is not None and vr <= _VOL_LIGHT and above60:
        verdict, level = "可能洗盘 · 观察别慌卖", "watch"
        why = "缩量回踩、还守着60日线，更像洗盘而非破位；守住关键位前别被情绪赶下车。"
    elif above20 is False:
        verdict, level = "趋势转弱 · 收紧止盈", "warn"
        why = "跌破生命线、量能不明确；收紧止盈、看能否快速站回20日线，站不回就减。"
    elif above20 and up:
        verdict, level = "持有 · 这是噪音", "hold"
        why = "价在20日线上方且均线向上——趋势锚没破，该做的不是操作，是什么都不做。"
    else:
        verdict, level = "持有 · 留意动能", "hold_soft"
        why = "仍站在20日线上方，但均线走平、动能转弱；继续持有，盯紧是否有效跌破。"

    return {
        "verdict": verdict, "level": level, "why": why, "checks": checks,
        "pnl": row.get("pnl"),
        "anchor_note": "卖出判定只看趋势 / 纪律位 / 量价，刻意不看你的成本价（市场不知道你买在哪）。",
    }
