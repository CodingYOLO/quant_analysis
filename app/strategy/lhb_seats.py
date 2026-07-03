"""龙虎榜席位明细 + 资金风格识别。

`top_inst`（已付费·5100可用）实际含**完整具名席位**，不止"机构专用"：
机构 + 沪深股通(北向) + 知名游资营业部 + 外资席位(高盛/瑞银…)。
本模块把一只票当日全部席位分类、识别游资，并按席位结构推断"资金风格"——
让"只看机构净买"升级为"机构/北向/游资/外资 谁在买谁在卖"的全貌。

诚实边界：游资席位按营业部名 + 自维护清单**近似**识别（非官方 hm_detail 昵称）；
机构席位 Tushare 匿名为"机构专用"无法区分具体机构。
"""

from __future__ import annotations

import pandas as pd

# —— 席位类型关键词 ——
_INST = "机构专用"
_NORTH = ("深股通", "沪股通")
_FOREIGN = ("高盛", "瑞银", "摩根", "美林", "野村", "汇丰", "瑞信", "巴克莱",
            "花旗", "德意志", "星展", "麦格理", "法国巴黎", "瑞士信贷")
# 知名游资席位（营业部关键词 → 地域/风格标签·近似识别，不点名具体个人游资）
_HOT_MONEY = [
    ("拉萨", "拉萨天团"), ("绍兴", "绍兴帮"), ("宁波", "宁波系"), ("温州", "温州帮"),
    ("佛山", "佛山系"), ("溧阳路", "溧阳路游资"), ("徐汇区高安路", "沪上游资"),
    ("上海江苏路", "沪上游资"), ("益田路", "益田路"), ("无锡清扬路", "清扬路"),
    ("成都北一环", "成都帮"), ("成都南一环", "成都帮"), ("成都北新街", "成都帮"),
    ("杭州上塘路", "杭州系"), ("杭州天目山路", "杭州系"), ("厦门湖滨南路", "厦门系"),
    ("中山", "中山帮"), ("深圳红岭中路", "深圳红岭"),
]

# —— 资金风格判定阈值（亿元·可校准）——
_NET_SIG = 0.2      # 净额"显著"门槛
_FOREIGN_SIG = 0.1


def classify_seat(exalter: str) -> dict:
    """单个席位分类。返回 {type, label, nickname}。

    type ∈ inst(机构) / north(北向) / foreign(外资) / hot(游资) / normal(普通营业部)。
    """
    s = exalter if isinstance(exalter, str) else ""     # 防 NaN(float)/None 让下游 `k in s` 崩溃
    if _INST in s:
        return {"type": "inst", "label": "机构", "nickname": ""}
    if any(k in s for k in _NORTH):
        return {"type": "north", "label": "北向", "nickname": ""}
    if any(k in s for k in _FOREIGN):
        return {"type": "foreign", "label": "外资", "nickname": next(k for k in _FOREIGN if k in s)}
    for kw, nick in _HOT_MONEY:
        if kw in s:
            return {"type": "hot", "label": "游资", "nickname": nick}
    return {"type": "normal", "label": "营业部", "nickname": ""}


def seat_rows(df_stock: pd.DataFrame) -> list[dict]:
    """把某只票的 top_inst 行 → 去重 + 分类 + 金额(亿)的席位列表，按净额降序。

    top_inst 同一席位会在买/卖两侧各列一次（完全重复）→ 按 (席位,买,卖) 去重。
    """
    if df_stock is None or df_stock.empty:
        return []
    seen, out = set(), []
    for _, r in df_stock.iterrows():
        ex = str(r.get("exalter") or "")
        buy = _num(r.get("buy")); sell = _num(r.get("sell")); net = _num(r.get("net_buy"))
        key = (ex, round(buy), round(sell))
        if key in seen:
            continue
        seen.add(key)
        c = classify_seat(ex)
        out.append({"exalter": ex, **c, "buy_yi": round(buy / 1e8, 2),
                    "sell_yi": round(sell / 1e8, 2), "net_yi": round(net / 1e8, 2),
                    "reason": str(r.get("reason") or "")})
    out.sort(key=lambda x: x["net_yi"], reverse=True)
    return out


def _num(v) -> float:
    x = pd.to_numeric(v, errors="coerce")
    return float(x) if pd.notna(x) else 0.0


def infer_style(seats: list[dict]) -> dict:
    """按席位结构推断资金风格（纯函数）。返回 {tags:[{text,level}], note}。"""
    def net(t):
        return sum(s["net_yi"] for s in seats if s["type"] == t)

    def buyers(t):
        return sum(1 for s in seats if s["type"] == t and s["net_yi"] > 0.05)

    def sellers(t):
        return sum(1 for s in seats if s["type"] == t and s["net_yi"] < -0.05)

    inst_net, north_net, hot_net, foreign_net = net("inst"), net("north"), net("hot"), net("foreign")
    tags: list[dict] = []
    if buyers("inst") >= 2 and inst_net > _NET_SIG:
        tags.append({"text": "机构抱团", "level": "strong"})
    if sellers("inst") >= 2 and inst_net < -_NET_SIG:
        tags.append({"text": "机构出货", "level": "warn"})
    if buyers("inst") >= 1 and sellers("inst") >= 1:
        tags.append({"text": "机构分歧", "level": "neu"})
    if hot_net > _NET_SIG:
        tags.append({"text": "游资主导", "level": "info"})
    elif hot_net < -_NET_SIG:
        tags.append({"text": "游资撤退", "level": "warn"})
    if north_net > _NET_SIG:
        tags.append({"text": "北向加仓", "level": "strong"})
    elif north_net < -_NET_SIG:
        tags.append({"text": "北向流出", "level": "warn"})
    if abs(foreign_net) > _FOREIGN_SIG:
        tags.append({"text": "外资参与", "level": "info"})
    if not tags:
        tags.append({"text": "多空混杂", "level": "neu"})
    note = f"机构净{inst_net:+.1f}亿 · 北向净{north_net:+.1f}亿 · 游资净{hot_net:+.1f}亿"
    return {"tags": tags, "note": note}


# —— 次日参考解读（席位结构 × 上榜原因 → 概率性剧本，不打包票）——
_REASON_STRONG = ("涨停", "涨幅")          # 涨停/涨幅上榜=强势抢筹
_REASON_WEAK = ("跌幅", "跌停")            # 下跌上榜=弱势
_REASON_DIVERGE = ("振幅", "换手")        # 振幅/换手=分歧活跃


def interpret_next_day(seats: list[dict], reason: str) -> dict:
    """按席位结构 + 上榜原因生成"次日参考"剧本（纯函数·概率口径不保证）。

    Returns: {tag(短标), level(bull/game/warn/watch), scenario(剧本), action(应对)}。
    位置(高/低位)需结合个股360同看，本判断只基于席位+上榜原因。
    """
    def net(t):
        return sum(s["net_yi"] for s in seats if s["type"] == t)

    def sellers(t):
        return sum(1 for s in seats if s["type"] == t and s["net_yi"] < -0.05)

    inst, north, hot = net("inst"), net("north"), net("hot")
    rs = reason if isinstance(reason, str) else ""      # 防 NaN(float)/None 让下游 `k in rs` 崩溃
    strong = any(k in rs for k in _REASON_STRONG)
    weak = any(k in rs for k in _REASON_WEAK)
    diverge = any(k in rs for k in _REASON_DIVERGE)

    # 优先级从高到低：先排雷（弱势/出货/接盘博弈），再看强势组合
    if weak and (inst < 0 or north < 0):
        return _verdict("警示", "warn", "弱势下跌上榜、资金在离场",
                        "次日大概率偏弱，反弹即减、不宜抄底")
    if inst < -_NET_SIG and sellers("inst") >= 2:
        return _verdict("警示", "warn", "机构席位在出货",
                        "次日上方有抛压，逢高减仓、别接刀")
    if inst < 0 and hot > _NET_SIG:
        return _verdict("博弈", "warn", "机构在撤、游资接盘",
                        "纯短线博弈，次日看承接力，追高风险大，宜轻仓快打、破位就走")
    if hot > _NET_SIG and strong:
        return _verdict("博弈", "game", "游资打板接力为主",
                        "次日情绪或高开冲高，但游资快进快出，做就别恋战、设好止损")
    if inst > _NET_SIG and net_inst_buyers(seats) >= 2:
        if north < -_NET_SIG:        # 机构买但北向在撤=分歧，别当纯利好
            return _verdict("分歧", "watch", "机构抱团买入、但北向资金在撤，多空分歧",
                            "次日看哪方占上风，外资抛压下别追高，回踩确认承接再跟")
        extra = "，北向同向加仓更积极" if north > _NET_SIG else ""
        return _verdict("偏多", "bull", "机构抱团抢筹、趋势资金进场" + extra,
                        "次日下方有支撑，回踩不破可跟、能拿几天（仍需结合位置）")
    if north > _NET_SIG and inst >= 0:
        return _verdict("偏多", "bull", "外资/北向中线加仓",
                        "偏积极，可作中线参考，回踩关注")
    if diverge or (inst > 0 and sellers("inst") >= 1):
        return _verdict("观望", "watch", "巨量分歧、多空打架",
                        "次日方向不明，等开盘看承接再定，不急于追")
    return _verdict("观望", "watch", "资金多空混杂、信号一般",
                    "次日观望为主，结合个股位置/板块再判断")


def net_inst_buyers(seats: list[dict]) -> int:
    return sum(1 for s in seats if s["type"] == "inst" and s["net_yi"] > 0.05)


def _verdict(tag: str, level: str, scenario: str, action: str) -> dict:
    return {"tag": tag, "level": level, "scenario": scenario, "action": action}
