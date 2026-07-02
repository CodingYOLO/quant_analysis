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

    macro_day = {m: [] for m in MACROS}                            # 各大类逐日净流入(亿)
    cap_day = {t: [] for t in CAP_NAMES}                           # 各市值档逐日净流入(亿)
    for d in dates:
        feat = _stock_features(provider, d)
        feat = feat[feat["net"].notna()].copy()
        feat["macro"] = feat.index.map(code2macro).fillna("其他")
        feat["cap"] = [(_cap_tier(c) if pd.notna(c) else None) for c in feat["circ"]]
        gm = feat.groupby("macro")["net"].sum()
        gc = feat[feat["cap"].notna()].groupby("cap")["net"].sum()
        for m in MACROS:
            macro_day[m].append(round(float(gm.get(m, 0.0)), 2))
        for t in CAP_NAMES:
            cap_day[t].append(round(float(gc.get(t, 0.0)), 2))

    macro = {m: {"cum": round(sum(macro_day[m]), 1), "series": macro_day[m]} for m in MACROS}
    cap = {t: {"cum": round(sum(cap_day[t]), 1), "series": cap_day[t]} for t in CAP_NAMES}
    return {
        "end": end, "window": len(dates), "dates": dates,
        "macro": dict(sorted(macro.items(), key=lambda kv: -kv[1]["cum"])),   # 按累计净流入降序
        "cap": cap,                                                           # 市值档保持大→微顺序
        "note": ("资金地图·描述性：近N日各大类/各市值档累计主力净流入(亿·Tushare官方估算·非龙虎榜真钱)。"
                 "行业大类与市值分档为**正交两条**(小盘电子仍计入科技·不重复计数)。"
                 "⚠️ 仅描述资金流向·**非买卖信号**；大类轮动判断需回测验证·不入决策层。"),
    }
