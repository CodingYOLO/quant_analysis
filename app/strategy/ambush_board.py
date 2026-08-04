"""
暗流低吸榜：把概念资金分成 真暗流 / 量价启动 / 假暗流(在撤) —— 借鉴稳智框架，
但**全部复用同花顺官方口径**（build_concept_persistent_flow · moneyflow_cnt_ths · DDE），
每个数都能在同花顺 APP 逐一核对。绝不另算资金（数据准确性第一）。

分档（纯客观·阈值可调）：
  🟢 真暗流   = 近3日净流入>0 且 今日还在进 且 价没涨(ret5<3%)   —— 持续低位吸筹
  🔵 量价启动 = 近3日净流入>0 且 今日在进 且 价开始动(3%~8%)     —— 暗流转明流
  🔴 假暗流   = 近5日>0 但 近3日≤0 或今日流出                   —— 之前进的钱在撤（别被5日数字骗，稳智精华）
  （已涨 ret5>8% 与 普通/流出 不进榜，仅计数）

诚实：资金=同花顺概念 DDE（估算·非龙虎榜真钱）；分档是客观结构描述·非预测/非荐股。
"""

from __future__ import annotations

import logging

from app.data.composite_provider import CompositeProvider
from app.strategy.concept_flow import build_concept_persistent_flow

logger = logging.getLogger(__name__)

_RET_UP = 8.0        # 已涨阈（近5日涨幅%·超过视为已涨·非低吸）
_RET_QUIET = 3.0     # 价没涨阈（真暗流要求 ret5 < 此·含下跌）
_MIN_MEMBERS = 5     # 概念成分下限
_SLIM_KEYS = ("concept", "cum3", "cum5", "today_net", "ret5", "pen5", "consec_days", "n", "lead")


def build_ambush_board(date: str, provider: CompositeProvider | None = None) -> dict:
    """构建暗流低吸榜。复用概念持续流入榜（同花顺官方）·只加分档·剔宽概念。"""
    prov = provider or CompositeProvider()
    rows = build_concept_persistent_flow(date, window=5, provider=prov)["rows"]
    buckets: dict[str, list] = {"real": [], "starting": [], "fake": [], "outflow": []}
    n_risen = 0
    for r in rows:
        if r.get("broad") or (r.get("n") or 0) < _MIN_MEMBERS:   # 暗流看 sharp 赛道·剔宽概念/太小概念
            continue
        g = _grade(r)
        if g == "risen":                                        # 已涨·非低吸·仅计数不进榜
            n_risen += 1
            continue
        buckets[g].append({**{k: r.get(k) for k in _SLIM_KEYS}, "grade": g})
    for k in ("real", "starting", "fake"):                      # 三档按近3日净流入降序(最猛在前)
        buckets[k].sort(key=lambda x: (x.get("cum3") if x.get("cum3") is not None else -1e9), reverse=True)
    buckets["outflow"].sort(key=lambda x: (x.get("cum3") if x.get("cum3") is not None else 1e9))  # 流出按流出额升序(最大流出在前·避雷)
    return {
        "date": date,
        "real": buckets["real"], "starting": buckets["starting"], "fake": buckets["fake"],
        "outflow": buckets["outflow"],
        "n_risen": n_risen, "n_outflow": len(buckets["outflow"]),
        "note": ("暗流分档全部基于同花顺官方概念资金(moneyflow_cnt_ths·DDE估算)·可在同花顺 APP 逐一核对。"
                 "🟢真暗流=近3日净流入+今日在进+价没涨(低位吸筹) · 🔵量价启动=资金进+价开始动 · "
                 "🔴假暗流=5日看着流入但近3日/今日在撤(别被5日数字骗)。"
                 "资金为估算·非龙虎榜真钱；分档为客观结构描述·非预测非荐股。"),
    }


# ── 🎯 个股暗流池（全市场·2026-08-04 页面重设计：埋伏的可执行单位是股票不是概念）────────
# 复用牛股发掘的全市场信号表 + 同一套 位置/资金/量价 评分（不另造口径）；
# 概念层分档保留作上下文，但主视图下沉到个股 + 证据链 + 行业月线方向交叉印证。
_POOL_GATES = {
    "flow3_min": 0.5,      # 3日主力净流入 ≥ 0.5亿（估算口径·太小无信息量）
    "chg7_max": 3.0,       # 近7日涨幅 ≤ 3%（价没动·还在暗处）
    "dist_high_min": 6.0,  # 距60日高点 ≥ 6%（不在冲高位·低吸导向）
    "top_n": 30,
}


def board_of(ts_code: str) -> str:
    """交易所板块判定（纯函数·按代码段）：主板/创业板/科创板/北交所。"""
    c = str(ts_code)
    if c.startswith(("688", "689")):
        return "科创"
    if c.startswith("30"):
        return "创业"
    if c.endswith(".BJ"):
        return "北交"
    if c.startswith(("60", "00")):
        return "主板"
    return "其他"


def stock_gate(rec: dict, g: dict | None = None) -> bool:
    """个股暗流硬门槛（纯函数·可单测）：资金在进 + 价没动 + 不在高位。"""
    g = g or _POOL_GATES
    flow = rec.get("main_flow_3d") or 0.0
    chg7 = rec.get("change_7d") or 0.0
    dist = rec.get("dist_high") or 0.0            # ≤0·越负离高点越远
    return flow >= g["flow3_min"] and chg7 <= g["chg7_max"] and -dist >= g["dist_high_min"]


def build_stock_ambush_pool(date: str, provider: CompositeProvider | None = None) -> dict:
    """全市场个股暗流池：信号表(200-5000亿·非ST·成交≥1亿) → 硬门槛 → 位置+资金+量价评分 Top30。

    每只带证据链与行业月线方向（读板块诊断缓存·资金×结构交叉印证）。零新增API调用
    （信号表按日缓存·财务深挖不在池级做——点进个股360看）。描述档·未回测·非荐股。
    """
    from app.strategy.bull_hunter import (_flow_score, _pos_score, _signal_table_cached,
                                          _vol_score)
    prov = provider or CompositeProvider()
    table = _signal_table_cached(date, prov)
    if table is None or table.empty:
        return {"ok": False, "date": date, "rows": [],
                "msg": f"{date} 全市场信号表为空（非交易日或数据未就绪）"}

    ind_dir = _industry_monthly_map(date)
    rows = []
    for ts, r in table.iterrows():
        rec = {"ts_code": ts, **{k: r[k] for k in r.index}}
        if not stock_gate(rec):
            continue
        fs, fe = _flow_score(rec)
        ps, pe = _pos_score(rec)
        vs, ve = _vol_score(rec)
        ind = str(rec.get("industry") or "")
        circ = rec.get("circ_mv_yi") or 0.0
        flow = rec.get("main_flow_3d") or 0.0
        rps = rec.get("rps50")
        rows.append({
            "ts_code": ts, "name": rec.get("name"), "industry": ind,
            "board": board_of(ts),
            "ind_monthly": (ind_dir.get(ind) or {}).get("monthly_dir"),
            "score": round(fs + ps + vs, 1),
            # 资金强度=3日流入/流通市值%(相对强度·并列破序)——门槛筛出的池普遍顶格45分·
            # 绝对亿数偏大盘·强度才能抓"小盘子被猛灌"
            "flow_intensity": round(flow / circ * 100, 2) if circ > 0 else None,
            "weak": bool(rps is not None and rps < 20),   # RPS<20=极弱·下跌途中接飞刀警示(只标不剔)
            "main_flow_3d": rec.get("main_flow_3d"), "change_7d": rec.get("change_7d"),
            "bias20": rec.get("bias20"), "dist_high": rec.get("dist_high"),
            "rps50": rec.get("rps50"), "vol_ratio": rec.get("vol_ratio"),
            "above_ma20": bool(rec.get("above_ma20")), "popular": bool(rec.get("popular")),
            "circ_mv_yi": rec.get("circ_mv_yi"),
            "evidence": f"{fe}；{pe}；{ve}",
        })
    rows.sort(key=lambda x: (-x["score"], -(x["flow_intensity"] or 0)))
    return {
        "ok": True, "date": date, "rows": rows[:_POOL_GATES["top_n"]],
        "n_pass": len(rows), "n_universe": int(len(table)),
        "gates": dict(_POOL_GATES),
        "note": ("池=市值200-5000亿·非ST·成交≥1亿(与选股线同口径)；门槛：3日主力净流入≥0.5亿(估算·"
                 "超大单+大单)+近7日涨幅≤3%+距60日高点≥6%。评分=资金初入+位置未过热+量价健康"
                 "(与牛股发掘同一套口径)。行业月线方向来自板块诊断缓存。描述档·未回测·非买卖建议；"
                 "财务/筹码深挖点代码进个股360。"),
    }


def _industry_monthly_map(date: str) -> dict:
    """{申万二级行业名: {monthly_dir}} ——读板块诊断已建缓存·只读不触发重建。"""
    import json

    from app.config import get_settings
    cdir = get_settings().cache_dir / "sector_mtf"
    cands = [cdir / f"industry_{date}_v2.json"]
    try:
        cands += sorted(cdir.glob("industry_*_v2.json"), reverse=True)   # 新→旧回退
    except Exception:
        pass
    for f in cands:
        if not f.exists():
            continue
        try:
            rows = json.loads(f.read_text(encoding="utf-8")).get("rows", [])
        except Exception:
            continue
        if rows:                                   # 空rows=盘中毒化缓存·跳过继续回退
            return {r["sector"]: {"monthly_dir": r.get("monthly_dir")} for r in rows}
    return {}


def _grade(r: dict) -> str:
    """单概念暗流分档（纯客观·同花顺官方多窗口）。real/starting 要求近3日与今日都在进（真持续）。"""
    cum3 = r.get("cum3") or 0.0
    cum5 = r.get("cum5") or 0.0
    today = r.get("today_net") or 0.0
    ret5 = r.get("ret5")
    ret5 = ret5 if ret5 is not None else 0.0
    if ret5 > _RET_UP:
        return "risen"
    if cum3 > 0 and today > 0:                          # 近3日+今日都净流入=真持续进
        return "real" if ret5 < _RET_QUIET else "starting"
    if cum5 > 0 and (cum3 <= 0 or today < 0):           # 5日看着流入但3日转负/今日流出=在撤
        return "fake"
    return "outflow"
