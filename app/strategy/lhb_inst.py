"""龙虎榜机构净买榜：每日"真机构钱"的日度信号。

龙虎榜「机构专用」席位是 A股仅存的个股级真实机构买卖披露
（2024-08 个股北向停发后，moneyflow 只是估算，机构席位才是真金白银）。
本模块聚合当日龙虎榜机构席位净买，列出机构真买 / 真卖的票，可按科技赛道过滤。

诚实边界：仅覆盖当日异动上榜股（非全市场）；机构净买≠后续必涨；
龙虎榜含次日博弈与对倒，需结合基本面 / 板块 / 估值同看。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.strategy.analysts import _TECH_INDUSTRIES

_DEFAULT_TOP = 30
_INST_SEAT = "机构专用"   # top_inst.exalter 中的机构席位标识


@dataclass
class InstFlow:
    """单只个股的龙虎榜机构席位汇总（金额单位亿元）。"""

    ts_code: str
    name: str
    industry: str        # 申万二级
    industry_l1: str     # 申万一级（科技赛道过滤用）
    net_yi: float        # 机构净买（买-卖）
    buy_yi: float        # 机构买入合计
    sell_yi: float       # 机构卖出合计
    seats: int           # 机构席位数
    reason: str          # 上榜原因
    is_tech: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _agg_inst(df: pd.DataFrame) -> dict[str, dict]:
    """聚合 top_inst『机构专用』席位 → {ts_code: {net,buy,sell,seats,reason}}（亿元）。

    纯函数：只依赖传入的 DataFrame，便于零网络单测。
    """
    out: dict[str, dict] = {}
    if df is None or df.empty or "exalter" not in df.columns:
        return out
    inst = df[df["exalter"] == _INST_SEAT]
    for _, r in inst.iterrows():
        ts = r["ts_code"]
        d = out.setdefault(ts, {"net": 0.0, "buy": 0.0, "sell": 0.0, "seats": 0, "reason": r.get("reason") or ""})
        d["net"] += _num(r.get("net_buy")) / 1e8
        d["buy"] += _num(r.get("buy")) / 1e8
        d["sell"] += _num(r.get("sell")) / 1e8
        d["seats"] += 1
    return out


def _num(v) -> float:
    """安全转浮点（元）。无效值记 0。"""
    x = pd.to_numeric(v, errors="coerce")
    return float(x) if pd.notna(x) else 0.0


def _name_maps(basic: pd.DataFrame) -> tuple[dict, dict, dict]:
    """从 get_stock_basic 构建 {ts_code: name/industry/industry_l1} 三张映射。"""
    if basic is None or basic.empty:
        return {}, {}, {}
    name = dict(zip(basic["ts_code"], basic.get("name", "")))
    ind = dict(zip(basic["ts_code"], basic.get("industry", "")))
    l1_col = basic["industry_l1"] if "industry_l1" in basic.columns else basic.get("industry", "")
    l1 = dict(zip(basic["ts_code"], l1_col))
    return name, ind, l1


def _is_tech(industry_l1: str) -> bool:
    """申万一级是否属科技赛道（电子/通信/计算机/电力设备/机械设备…）。"""
    return any(k in (industry_l1 or "") for k in _TECH_INDUSTRIES)


def build_inst_board(
    provider: CompositeProvider,
    trade_date: str,
    top: int = _DEFAULT_TOP,
    tech_only: bool = False,
) -> dict:
    """构建当日龙虎榜机构净买/净卖榜。

    Args:
        provider: 数据访问（依赖注入·便于单测打桩）。
        trade_date: 交易日 YYYYMMDD。
        top: 买/卖榜各取前 N。
        tech_only: 仅留科技赛道（按申万一级）。

    Returns:
        {date, tech_only, buys:[...], sells:[...], n_total}
    """
    try:
        df = provider.get_lhb_inst(trade_date)
    except Exception:
        df = None
    agg = _agg_inst(df)
    try:
        name_m, ind_m, l1_m = _name_maps(provider.get_stock_basic())
    except Exception:
        name_m, ind_m, l1_m = {}, {}, {}

    flows: list[InstFlow] = []
    for ts, d in agg.items():
        l1 = l1_m.get(ts, "")
        flows.append(InstFlow(
            ts_code=ts, name=name_m.get(ts, ts), industry=ind_m.get(ts, ""), industry_l1=l1,
            net_yi=round(d["net"], 2), buy_yi=round(d["buy"], 2), sell_yi=round(d["sell"], 2),
            seats=d["seats"], reason=d["reason"], is_tech=_is_tech(l1),
        ))
    if tech_only:
        flows = [f for f in flows if f.is_tech]

    buys = sorted([f for f in flows if f.net_yi > 0], key=lambda x: -x.net_yi)[:top]
    sells = sorted([f for f in flows if f.net_yi < 0], key=lambda x: x.net_yi)[:top]
    return {
        "date": trade_date, "tech_only": tech_only, "n_total": len(flows),
        "buys": [f.to_dict() for f in buys],
        "sells": [f.to_dict() for f in sells],
    }
