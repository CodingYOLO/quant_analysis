"""
M5：板块全景看板分类引擎（纯因子，无 LLM）。

读中枢宽表 theme_heat_all_in_one，按 A 股主线投资特性把板块打到三类
互不排斥的诊断视角（同一板块可同时命中多类）：

  - 轮动上行：主力持续净流入 + 价在涨 + 结构健康 → 在途主线，可追。
  - 低吸观察：中期趋势仍在、结构未破，但今日出现分歧（当日主力净流出或价格回调）
              → 等回踩的低吸候选。
  - 高位风险：涨幅居前 + 超买/拥挤 → 高位派发风险，防接盘。

设计要点（为何这样判，而非简单照搬绝对阈值）：
  * 板块级「7日涨幅」是众多成分股的平均，被严重稀释（实测全市场 90% 分位仅 ~3%），
    用绝对值 ≥10% 几乎永不命中 → 高位风险恒空。故「涨幅居前/拥挤居前」改用
    **当日横截面相对分位**（regime-adaptive），结构性门槛（广度/资金方向）才用绝对值。
  * 指数样本股（沪深300/上证50/上证180 等）并非可操作主题，全栏剔除，避免污染。

所有阈值集中在 _THRESHOLDS，便于对照原站回归校准。
"""

from __future__ import annotations

import logging

from app.data.theme_heat_db import get_themes, latest_trade_date

logger = logging.getLogger(__name__)

# ── 分类阈值（配置化；相对分位部分会随当日行情自适应）────────────────────────
_THRESHOLDS = {
    # 轮动上行
    "rotate_breadth_ma20": 50.0,   # 结构健康：半数以上成分站上 MA20
    "rotate_mf3_pctile": 0.55,     # 主力3日净流入需进入当日前 45%（剔除边际流入噪音）
    # 低吸观察
    "dip_breadth_ma20": 45.0,      # 结构未破（不低吸已破位板块）
    # 高位风险
    "risk_pct5_pctile": 0.75,      # 涨幅居前：5日涨幅进入当日前 25%
    "risk_pct3_pctile": 0.80,      # 或 3日涨幅进入当日前 20%
    "risk_top100_pctile": 0.85,    # 拥挤：Top100 占比进入当日前 15%
    "risk_top100_floor": 10.0,     # 拥挤绝对下限（分位过低时兜底，%）
    "risk_breadth_overbought": 70.0,  # 极度超买：>70% 成分站上 MA20
    # 资金暗流（资金领先价格·吴川"资金进+价没涨"埋伏）
    "ambush_pct5_max": 3.0,        # 5日涨幅 < 此 = 价还没被推动（上限·滞涨）
    "ambush_pct5_min": -3.0,       # 5日涨幅 ≥ 此 = 价走平未跌穿；**下限·避免把大跌(−6%/−8%)误判成"没涨"**
    "ambush_mf5_pctile": 0.60,     # 近5日净流入进当日前 40%（显著·剔"+1亿"边际噪音·与其他栏一致用相对分位）
    "ambush_breadth_min": 45.0,    # 结构未破（≥45%成分站上MA20）·真吸筹埋伏·非破位下跌板块（接刀）
    # 通用
    "min_sample": 3,               # 成分过少的板块不参与诊断（统计不可靠）
}

# 指数样本股 / 宽基成份并非可操作主题，全栏剔除
_EXCLUDE_KEYWORDS = ("样本股", "成份", "成分", "指数")


# ── 数值与分位工具 ──────────────────────────────────────────────────────────
def _num(v, default: float = float("-inf")) -> float:
    """None/非数 → default（默认 -inf，使 ≥ 阈值判断安全地不命中）。"""
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _percentile(values: list[float], q: float) -> float:
    """当日横截面分位（线性最近秩）。空集合返回 +inf（使任何 ≥ 判断不命中）。"""
    vals = sorted(v for v in values if v is not None and v != float("-inf"))
    if not vals:
        return float("inf")
    idx = min(len(vals) - 1, int(len(vals) * q))
    return vals[idx]


def _build_context(rows: list[dict]) -> dict:
    """预计算当日横截面阈值（分位 + 中位），供各诊断规则共享。"""
    mf3 = [_num(r.get("money_flow_3d")) for r in rows]
    mf5 = [_num(r.get("money_flow_5d")) for r in rows]
    pct5 = [_num(r.get("pct_chg_5d")) for r in rows]
    pct3 = [_num(r.get("pct_chg_3d")) for r in rows]
    pct1 = [_num(r.get("pct_chg_1d")) for r in rows]
    top100 = [_num(r.get("top100_ratio")) for r in rows]
    t = _THRESHOLDS
    return {
        "mf3_cut": _percentile(mf3, t["rotate_mf3_pctile"]),
        "mf5_cut": _percentile(mf5, t["ambush_mf5_pctile"]),   # 暗流：净流入显著门槛（剔噪）
        "pct5_cut": _percentile(pct5, t["risk_pct5_pctile"]),
        "pct3_cut": _percentile(pct3, t["risk_pct3_pctile"]),
        "pct1_median": _percentile(pct1, 0.50),   # 当日涨幅中位（判定「相对走弱」）
        "top100_cut": max(_percentile(top100, t["risk_top100_pctile"]),
                          t["risk_top100_floor"]),
    }


# ── 三类诊断规则（纯函数，便于单测）─────────────────────────────────────────
def _is_rotate(r: dict, ctx: dict) -> bool:
    """轮动上行：主力3日净流入(且居前) + 3日上涨 + 结构健康。"""
    t = _THRESHOLDS
    return (
        _num(r.get("money_flow_3d")) > 0
        and _num(r.get("money_flow_3d")) >= ctx["mf3_cut"]
        and _num(r.get("pct_chg_3d")) > 0
        and _num(r.get("breadth_ma20")) >= t["rotate_breadth_ma20"]
    )


def _is_dip(r: dict, ctx: dict) -> bool:
    """
    低吸观察：中期趋势/资金仍在 + 结构未破 + 今日分歧（资金流出 或 涨幅相对走弱）。

    「今日分歧」放宽到「涨幅低于当日中位」而非必须下跌——普涨日里强势主线
    若今日明显跑输大盘，往往是资金获利分歧、待回踩的低吸点。过热板块在
    _classify 中另行剔除（不低吸已拥挤板块）。
    """
    t = _THRESHOLDS
    mid_trend_in = (
        _num(r.get("money_flow_5d")) > 0
        and _num(r.get("pct_chg_5d")) > 0
    )
    today_diverge = (
        _num(r.get("money_flow_1d"), float("inf")) < 0
        or _num(r.get("pct_chg_1d"), float("inf")) < ctx["pct1_median"]
    )
    return (
        mid_trend_in
        and today_diverge
        and _num(r.get("breadth_ma20")) >= t["dip_breadth_ma20"]
    )


def _is_risk(r: dict, ctx: dict) -> bool:
    """高位风险：涨幅居前(5日或3日) 且 (极度超买 或 拥挤居前)。"""
    t = _THRESHOLDS
    big_run = (
        _num(r.get("pct_chg_5d")) >= ctx["pct5_cut"]
        or _num(r.get("pct_chg_3d")) >= ctx["pct3_cut"]
    )
    overheated = (
        _num(r.get("breadth_ma20")) >= t["risk_breadth_overbought"]
        or _num(r.get("top100_ratio")) >= ctx["top100_cut"]
    )
    return big_run and overheated


def _is_ambush(r: dict, ctx: dict) -> bool:
    """资金暗流（资金领先价格·吴川"资金进+价没涨"埋伏）——四条同时满足才算，剔除误判/噪音：
      ① 近5日主力净流入 **显著**（横截面前 40%·剔"+1亿"边际噪音）；
      ② 5日涨幅在 **[-3%, 3%) 走平区间**（价没被推动·**非大跌**——避免把跌6%/8%的板块误标"没涨"）；
      ③ 今日资金未净流出；
      ④ **结构未破**（≥45%成分站上MA20）——真吸筹埋伏，而非破位下跌的"接刀"板块。
    已拥挤/高位者在 _classify 另行排除。"""
    t = _THRESHOLDS
    pct5 = _num(r.get("pct_chg_5d"), 99.0)
    return (
        _num(r.get("money_flow_5d")) > 0
        and _num(r.get("money_flow_5d")) >= ctx["mf5_cut"]            # ① 净流入显著
        and t["ambush_pct5_min"] <= pct5 < t["ambush_pct5_max"]      # ② 价走平·非大跌
        and _num(r.get("money_flow_1d"), 0.0) >= 0                    # ③ 今日未净流出
        and _num(r.get("breadth_ma20")) >= t["ambush_breadth_min"]  # ④ 结构未破·非接刀
    )


# ── 主入口 ──────────────────────────────────────────────────────────────────
def build_sectorscope(date: str = "",
                      theme_types: tuple[str, ...] = ("industry", "concept")) -> dict:
    """
    构建板块全景数据。

    Args:
        date: 交易日 YYYYMMDD；空则取宽表最近已计算日。
        theme_types: 纳入的板块类型。

    Returns:
        {ok, available, date, rows, buckets:{rotate,dip,risk}}。
        rows 含 signal/signals。无数据时 available=False（不展示旧/假数据）。
    """
    d = (date or "").replace("-", "") or (latest_trade_date() or "")
    if not d:
        return {"available": False, "date": "", "rows": [], "buckets": {},
                "msg": "宽表尚未计算，请先运行 python -m app.run wide"}

    rows = _load_rows(d, theme_types)
    if not rows:
        return {"available": False, "date": d, "rows": [], "buckets": {},
                "msg": f"{d} 宽表未计算（数据缺失，不展示旧/假数据）"}

    ctx = _build_context(rows)
    rotate, dip, risk, ambush = _classify(rows, ctx)
    surge, decay = _stage_tops(rows)

    return {
        "available": True,
        "date": d,
        "rows": rows,
        "buckets": {
            "rotate": rotate[:12],
            "dip": dip[:12],
            "risk": risk[:12],
            "ambush": ambush[:15],   # 资金暗流（资金进+价没涨）
        },
        # 板块阶段·趋势动量（对标吴川：趋势强度 heat_score + 3日变化 Δ）
        "stage": {
            "surge": surge[:10],   # 主升候选：升温/趋势，按强度
            "decay": decay[:10],   # 退潮预警：退潮，按 Δ 最快衰减
        },
    }


# ── 板块阶段·趋势动量（对标吴川 板块阶段识别：trend_score + 3日变化）──────────
# 复用宽表自带字段：heat_score(趋势强度 0~100) / heat_score_delta_3d(3日变化Δ) / phase
_SURGE_PHASES = ("升温", "趋势")


def _stage_tops(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    主升候选 / 退潮预警 两个排行（吴川式生命周期视角，与三栏诊断互补）。

    主升候选：phase∈{升温,趋势}，按强度 heat_score 降序（同档 Δ 大者优先）。
    退潮预警：phase==退潮，按 3 日变化 Δ 升序（衰减最快在前），同档低强度优先。
    """
    surge = [r for r in rows if r.get("phase") in _SURGE_PHASES]
    decay = [r for r in rows if r.get("phase") == "退潮"]
    surge.sort(key=lambda r: (_num(r.get("heat_score")),
                              _num(r.get("heat_score_delta_3d"))), reverse=True)
    decay.sort(key=lambda r: (_num(r.get("heat_score_delta_3d"), 0.0),
                              _num(r.get("heat_score"), 100.0)))
    return surge, decay


def _load_rows(d: str, theme_types: tuple[str, ...]) -> list[dict]:
    """读宽表并剔除指数样本股、成分过少的板块。"""
    rows: list[dict] = []
    for t in theme_types:
        rows.extend(get_themes(d, t))
    min_n = _THRESHOLDS["min_sample"]
    return [
        r for r in rows
        if not any(k in r["theme_name"] for k in _EXCLUDE_KEYWORDS)
        and _num(r.get("sample_count"), 0) >= min_n
    ]


# 宽概念/宽基指数去噪（板块全景只留可操作赛道；申万行业一律保留）
_INDEX_LIKE = ("中国AI", "同花顺出海", "同花顺漂亮", "同花顺新质", "新质50", "国有大型银行",
               "股份制银行", "科创50", "创业板指", "沪深300", "上证50", "上证180")


def _is_broad_theme(r: dict) -> bool:
    """宽概念/宽基指数/持股状态 → 剔（非可操作赛道·中国AI50/漂亮100/国有大型银行/国家大基金持股等）。"""
    if r.get("theme_type") == "industry":
        return False
    from app.strategy.concept_flow import _broad_reason
    nm = str(r.get("theme_name") or "")
    return bool(_broad_reason(nm)) or any(p in nm for p in _INDEX_LIKE)


def _classify(rows: list[dict], ctx: dict) -> tuple[list, list, list, list]:
    """三栏诊断（**排名制**·取当日相对最符合各类特征的板块·弱势日也有内容；剔宽概念）。
    互斥优先级：高位风险 > 轮动上行 > 低吸观察（暗流可叠加）。返回 (rotate, dip, risk, ambush)。"""
    pool = [r for r in rows if not _is_broad_theme(r)]            # 剔宽概念/宽基指数
    # 高位风险：涨幅高 + 拥挤度高（拥挤优先·最需警惕·先占）
    risk = sorted(pool, key=lambda r: (_num(r.get("top100_ratio")), _num(r.get("pct_chg_5d"))),
                  reverse=True)[:12]
    rids = {id(r) for r in risk}
    # 轮动上行：资金流入 + 强度共振（近3日资金 → MA20广度）·排除高位
    rotate = sorted((r for r in pool if id(r) not in rids),
                    key=lambda r: (_num(r.get("money_flow_3d")), _num(r.get("breadth_ma20"))),
                    reverse=True)[:12]
    seen = rids | {id(r) for r in rotate}
    # 低吸观察：回撤（近3日跌）但结构未破（MA20广度 → 中期资金）·排除高位/轮动
    dip = sorted((r for r in pool if id(r) not in seen and _num(r.get("pct_chg_3d")) < 0),
                 key=lambda r: (_num(r.get("breadth_ma20")), _num(r.get("money_flow_5d"))),
                 reverse=True)[:12]
    # 资金暗流：保留原谓词（资金进+价没涨·非高位）·按近5日净流入排序
    ambush = sorted((r for r in pool if id(r) not in rids and _is_ambush(r, ctx)),
                    key=lambda r: _num(r.get("money_flow_5d")), reverse=True)
    # 写回 signal 标签（id 判归属·互斥优先 高位>轮动>低吸；暗流可叠加）
    rotids, dids, aids = {id(r) for r in rotate}, {id(r) for r in dip}, {id(r) for r in ambush}
    for r in rows:
        flags = []
        if id(r) in rids:
            flags.append("高位风险")
        elif id(r) in rotids:
            flags.append("轮动")
        elif id(r) in dids:
            flags.append("低吸")
        if id(r) in aids:
            flags.append("资金暗流")
        r["signal"] = flags[0] if flags else ""
        r["signals"] = flags
    return rotate, dip, risk, ambush
