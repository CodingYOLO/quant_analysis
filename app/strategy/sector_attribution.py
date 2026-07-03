"""
大类资金归因·资金地图（描述性·**非买卖信号**）。

比板块状态机高一层的"钱往哪走"总量视角：近 N 日各**大类**累计主力净流入，用于人工辨识
"真风格切换 vs 板块内部高低切"（吴川用"科技15日+550亿 vs 传统+75亿"证伪科技→传统轮动）。

两条**正交**分组（各自完整分区全市场·不重复计数·不漏算）：
  1. **行业大类**：申万一级 → 8 大类（科技/高端制造/医药/消费/周期/金融地产/公用/其他）。
     小市值电子股仍算科技——**不把小微盘从行业里抠出来**。
  2. **市值分档**：按当日流通市值分 大盘/中盘/小盘/微盘（point-in-time·与行业正交并列展示）。

⚠️ 纪律：本层**只描述资金流向·不下"该买某大类"结论**。大类轮动作为择时判断属**未验证信号**，
   须走状态机同样的回测验证流程，**不得混入决策层**（[[no-directional-recommendations]]）。
口径：主力净流入=Tushare官方(超大单+大单)估算·非龙虎榜真机构钱。point-in-time·只用≤end数据。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.factors.breadth_qfq import _recent_trade_dates
from app.strategy.sector_metrics import _stock_features
from app.strategy.sw_membership import load_history, members_asof

logger = logging.getLogger(__name__)

# 申万一级 → 行业大类（8类·兜底/未映射→其他）
_L1_TO_MACRO = {
    "电子": "科技", "计算机": "科技", "通信": "科技", "传媒": "科技",
    "电力设备": "高端制造", "机械设备": "高端制造", "国防军工": "高端制造", "汽车": "高端制造",
    "医药生物": "医药",
    "食品饮料": "消费", "家用电器": "消费", "美容护理": "消费", "商贸零售": "消费",
    "社会服务": "消费", "纺织服饰": "消费", "农林牧渔": "消费",
    "有色金属": "周期", "钢铁": "周期", "煤炭": "周期", "石油石化": "周期",
    "基础化工": "周期", "建筑材料": "周期",
    "银行": "金融地产", "非银金融": "金融地产", "房地产": "金融地产", "建筑装饰": "金融地产",
    "公用事业": "公用", "交通运输": "公用", "环保": "公用",
}
MACROS = ("科技", "高端制造", "医药", "消费", "周期", "金融地产", "公用", "其他")

# 市值分档（流通市值·亿）：与行业大类正交·另一条并列
_CAP_TIERS = (("大盘≥500亿", 500.0), ("中盘100-500亿", 100.0),
              ("小盘30-100亿", 30.0), ("微盘<30亿", 0.0))
CAP_NAMES = tuple(t[0] for t in _CAP_TIERS)


def _cap_tier(circ_yi: float) -> str:
    for name, lo in _CAP_TIERS:
        if circ_yi >= lo:
            return name
    return _CAP_TIERS[-1][0]


def build_flow_map(end: str, window: int = 15, provider: CompositeProvider | None = None) -> dict:
    """近 window 日 资金地图：各行业大类 + 各市值分档 的累计主力净流入(亿)·逐日序列。描述性·非信号。"""
    provider = provider or CompositeProvider()
    dates = _recent_trade_dates(provider, end, window)
    if not dates:
        raise ValueError(f"{end} 无交易日")

    # 个股 → 行业大类（申万一级映射·end 时点成分）
    l1map = members_asof(load_history(provider), end, "L1", exclude_junk=False)
    code2macro = {c: _L1_TO_MACRO.get(l1, "其他") for l1, codes in l1map.items() for c in codes}

    # 逐日按大类/市值档聚合 净流入 + 流通市值 + 中位涨幅（供渗透率% + 价-资金背离）
    grp = {m: {"net": [], "circ": [], "pct": []} for m in MACROS}
    cgrp = {t: {"net": [], "circ": [], "pct": []} for t in CAP_NAMES}
    for d in dates:
        feat = _stock_features(provider, d)
        feat = feat[feat["net"].notna()].copy()
        feat["macro"] = feat.index.map(code2macro).fillna("其他")
        feat["cap"] = [(_cap_tier(c) if pd.notna(c) else None) for c in feat["circ"]]
        _accum(grp, feat, "macro", MACROS)
        _accum(cgrp, feat[feat["cap"].notna()], "cap", CAP_NAMES)

    macro = {m: _grp_metrics(grp[m]) for m in MACROS}
    cap = {t: _grp_metrics(cgrp[t]) for t in CAP_NAMES}
    return {
        "end": end, "window": len(dates), "dates": dates,
        "macro": dict(sorted(macro.items(), key=lambda kv: -(kv[1]["pen15"] or -99))),  # 按渗透率%降序(可比)
        "cap": cap,
        "note": ("资金地图·描述性：多跨度(近5/10/15日)主力净流入的**渗透率%**(占流通市值·跨体量可比) + 净额(亿)。"
                 "**边际**区分'真转流入(一阶转正)'与'流出收窄(二阶)'；**价-资金背离**=钱进价跌(暗流)/价涨钱撤(顶背离)。"
                 "行业大类与市值分档正交·⚠️仅描述·**非买卖信号**·大类轮动需回测验证不入决策层。"),
    }


def _accum(store: dict, feat, key: str, names) -> None:
    """把当日 feat 按 key(macro/cap) 聚合 净流入(sum)/流通市值(sum)/中位涨幅 追加到序列。"""
    gn = feat.groupby(key)["net"].sum()
    gc = feat.groupby(key)["circ"].sum()
    gp = feat.groupby(key)["pct"].median()
    for nm in names:
        store[nm]["net"].append(round(float(gn.get(nm, 0.0)), 2))
        store[nm]["circ"].append(round(float(gc.get(nm, 0.0)), 2))
        v = gp.get(nm)
        store[nm]["pct"].append(None if v is None or (isinstance(v, float) and v != v) else round(float(v), 3))


def _grp_metrics(g: dict) -> dict:
    """净流入/流通市值/涨幅序列 → 多跨度 净额+渗透率% · 近5日涨 · 边际(真转正/收窄) · 价-资金背离。"""
    net, circ, pct = g["net"], g["circ"], g["pct"]
    circ_now = next((c for c in reversed(circ) if c), 0) or 1               # 当前流通市值(分母·亿)
    cum = lambda n: round(sum(net[-n:]), 1) if net else 0.0                 # noqa: E731
    pen = lambda n: round(cum(n) / circ_now * 100, 3)                       # 渗透率%=累计净流入/流通市值 noqa: E731
    price5 = _compound_pct([p for p in pct[-5:] if p is not None])          # 近5日板块涨(复利·中位)
    return {
        "cum5": cum(5), "cum10": cum(10), "cum15": cum(len(net)),
        "pen5": pen(5), "pen10": pen(10), "pen15": pen(len(net)),
        "price5": price5, "series": net,
        "margin": _margin(net), "diverge": _diverge(cum(5), price5),
    }


def _margin(series: list) -> dict:
    """边际：区分'真转流入(一阶导·近5实际净流入)'与'流出收窄(二阶导·仍净流出但放缓)'。strong=真拐点。

    None-safe：概念板块某日可能缺数据(None)·只对非空求和/求均(行业net无None·行为不变)。
    """
    last5 = [x for x in series[-5:] if x is not None]
    allv = [x for x in series if x is not None]
    if len(allv) < 8 or not last5:
        return {"arrow": "→", "text": "", "strong": False}
    cum5, cum_all = sum(last5), sum(allv)
    avg5, avg_all = cum5 / len(last5), cum_all / len(allv)
    if cum_all < 0 and cum5 > 0:                                            # 一阶导转正=真转流入(最醒目)
        return {"arrow": "↑", "text": "真转流入(一阶转正)", "strong": True}
    if cum_all < 0:                                                         # 仍净流出：只是二阶导变化
        return ({"arrow": "↗", "text": "流出收窄(仍净流出)", "strong": False} if avg5 > avg_all
                else {"arrow": "↓", "text": "流出加剧", "strong": False})
    if cum_all > 0 and cum5 < 0:
        return {"arrow": "↓", "text": "近5日转流出", "strong": False}
    return ({"arrow": "↗", "text": "流入加速", "strong": False} if avg5 > avg_all
            else {"arrow": "↘", "text": "流入转弱", "strong": False})


def _diverge(net5, price5) -> dict:
    """近5日 价-资金背离（这套系统的灵魂）：钱进价跌=暗流·价涨钱撤=顶背离(资金撤)。"""
    if net5 is None or price5 is None:
        return {"tag": "", "text": ""}
    if net5 > 0 and price5 < -1:
        return {"tag": "暗流", "text": "钱进价跌·埋伏"}
    if net5 < 0 and price5 > 1:
        return {"tag": "顶背离", "text": "价涨钱撤·警惕"}
    return {"tag": "", "text": ""}


def _compound_pct(pcts: list):
    """复利叠加日涨幅 → 区间涨幅%(中位口径)。空→None。"""
    if not pcts:
        return None
    prod = 1.0
    for p in pcts:
        prod *= (1 + p / 100.0)
    return round((prod - 1) * 100, 2)
