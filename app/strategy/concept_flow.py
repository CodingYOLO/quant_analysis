"""
概念板块资金流仪表盘数据聚合（同花顺概念口径）。

与 industry_flow（Tushare 110 行业，自聚合）互补：
本模块直连 Tushare 官方接口 moneyflow_cnt_ths（同花顺概念资金流），
一次调用即返回每个概念的涨跌幅 / 净额 / 领涨股 / 成分数，
不依赖被封的东方财富概念接口，国内服务器可直连。

产出（指定交易日）：
  - KPI：概念数 / 平均涨跌幅 / 净流入概念数 / 净流出概念数 / 全市场概念净额
  - 概念明细：按净额排序，含涨跌幅 / 净额 / 成分数 / 领涨股 / 排名 / 排名变化

数据：走 CompositeProvider 内的 Tushare pro_api（与 market_extras 同一约定）。
单位：net_amount 为 Tushare 官方口径「净额（亿元）」。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.nodes.quick_report import _recent_trade_dates

logger = logging.getLogger(__name__)


def _fetch_concept_flow(pro, date: str) -> pd.DataFrame:
    """拉取并规范化单个交易日的同花顺概念资金流。空表返回空 DataFrame。"""
    try:
        df = pro.moneyflow_cnt_ths(trade_date=date)
    except Exception as e:
        logger.warning("[概念] moneyflow_cnt_ths 拉取失败: %s", e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    for col in ("pct_change", "net_amount", "company_num", "pct_change_stock"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _build_rows(df: pd.DataFrame, rank_change: dict[str, int]) -> list[dict]:
    """将规范化后的概念资金流表转为前端行记录（按净额降序）。"""
    df = df.sort_values("net_amount", ascending=False).reset_index(drop=True)
    rows = []
    for i, r in df.iterrows():
        name = str(r["name"])
        lead = str(r.get("lead_stock", "") or "")
        lead_pct = r.get("pct_change_stock")
        lead_str = f"{lead} {lead_pct:+.1f}%" if lead and pd.notna(lead_pct) else lead
        rows.append({
            "concept": name,
            "code": str(r["ts_code"]),
            "pct_chg": round(float(r["pct_change"]), 2) if pd.notna(r["pct_change"]) else 0.0,
            "net_amount": round(float(r["net_amount"]), 2) if pd.notna(r["net_amount"]) else 0.0,
            "company_num": int(r["company_num"]) if pd.notna(r.get("company_num")) else 0,
            "lead": lead_str,
            "rank": i + 1,
            "rank_change": int(rank_change.get(name, 0)),
        })
    return rows


def _rank_change_map(provider, pro, date: str, today_names_order: list[str]) -> dict[str, int]:
    """计算各概念今日 vs 上一交易日的净额排名变化（正=排名上升）。"""
    try:
        prev_dates = _recent_trade_dates(provider, date, n=2)
        if len(prev_dates) < 2:
            return {}
        prev_df = _fetch_concept_flow(pro, prev_dates[-2])
        if prev_df.empty:
            return {}
        prev_df = prev_df.sort_values("net_amount", ascending=False).reset_index(drop=True)
        prev_rank = {str(r["name"]): i + 1 for i, r in prev_df.iterrows()}
        return {
            name: (prev_rank[name] - (i + 1))
            for i, name in enumerate(today_names_order)
            if name in prev_rank
        }
    except Exception as e:
        logger.debug("[概念] 排名变化计算失败: %s", e)
        return {}


def build_concept_dashboard(date: str) -> dict:
    """
    构建概念资金流仪表盘数据（指定交易日）。

    Args:
        date: 交易日 YYYYMMDD

    Returns:
        {"date", "kpi": {...}, "rows": [...]}，结构对齐 industry_flow 便于前端复用。

    Raises:
        ValueError: 当日无概念资金流数据（非交易日或数据未入库）。
    """
    provider = CompositeProvider()
    pro = provider._ts._api

    df = _fetch_concept_flow(pro, date)
    if df.empty:
        raise ValueError(f"{date} 概念资金流为空（非交易日，或收盘后数据尚未入库）")

    sorted_names = df.sort_values("net_amount", ascending=False)["name"].astype(str).tolist()
    rank_change = _rank_change_map(provider, pro, date, sorted_names)
    rows = _build_rows(df, rank_change)

    net = df["net_amount"]
    kpi = {
        "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "concept_count": int(len(df)),
        "avg_pct": round(float(df["pct_change"].mean()), 2),
        "inflow_count": int((net > 0).sum()),
        "outflow_count": int((net < 0).sum()),
        "total_net": round(float(net.sum()), 2),
    }
    return {"date": date, "kpi": kpi, "rows": rows}


# ── 概念板块·多日资金流动特征（供板块诊断"资金层"嗅题材热点·纯描述非信号）──────────────
def _cross_z_map(m: dict) -> dict:
    """某日概念净额的横截面稳健 z-score（中位/MAD）·让概念强度可比。<5→全None。"""
    import numpy as np
    vals = [v for v in m.values() if v is not None]
    if len(vals) < 5:
        return {k: None for k in m}
    a = np.array(vals, dtype=float)
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med))) * 1.4826 or (float(a.std()) or 1.0)
    # 裁剪 ±4：概念数量多、离群极端·防个别概念 z 爆表主导升温榜(与行业可比)
    return {k: (round(max(-4.0, min(4.0, (v - med) / mad)), 2) if v is not None else None)
            for k, v in m.items()}


def build_concept_flow_features(end: str, window: int = 14, provider=None, min_company: int = 5) -> list[dict]:
    """概念板块近 window 日资金流动特征（同花顺 moneyflow_cnt_ths 官方净额·亿）。

    纯描述·**嗅当下题材热点**（机器人/CPO/减速器等·不在申万L2里）·非回测信号（不受成分漂移限制）。
    返回与行业统一 schema 的 flow 行：{sector,kind='概念',net5,penz_seq,pen_accel,flow_margin,ret5,n}。
    """
    from app.data.cache import cached_daily
    from app.factors.theme_wide import _is_junk_concept
    from app.strategy.sector_attribution import _compound_pct, _margin
    from app.strategy.sector_diagnosis import _smooth_seq
    provider = provider or CompositeProvider()
    pro = provider._ts._api
    dates = _recent_trade_dates(provider, end, window)
    if not dates:
        return []

    net_by, pct_by, comp = [], [], {}
    for d in dates:
        df = cached_daily("ths_concept_flow", d, lambda d=d: _fetch_concept_flow(pro, d))  # 按日缓存
        nmap, pmap = {}, {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                nm = str(r.get("name", "") or "")
                if not nm or nm == "nan" or _is_junk_concept(nm):
                    continue
                na, pc, cn = r.get("net_amount"), r.get("pct_change"), r.get("company_num")
                nmap[nm] = float(na) if pd.notna(na) else None
                pmap[nm] = float(pc) if pd.notna(pc) else None
                if pd.notna(cn):
                    comp[nm] = int(cn)
        net_by.append(nmap)
        pct_by.append(pmap)

    z_by = [_cross_z_map(m) for m in net_by]
    names = set().union(*[set(m) for m in net_by]) if net_by else set()
    need = max(5, int(window * 0.5))
    rows = []
    for nm in names:
        if comp.get(nm, 0) < min_company:                          # 剔太小的概念(成分<min)
            continue
        nets = [m.get(nm) for m in net_by]
        if sum(1 for x in nets if x is not None) < need:            # 数据太少跳过
            continue
        zfull = [m.get(nm) for m in z_by]
        accel = (round(zfull[-1] - zfull[-2], 2)
                 if zfull[-1] is not None and zfull[-2] is not None else None)
        pcts = [m.get(nm) for m in pct_by]
        f5d = round(sum(x for x in nets[-5:] if x is not None), 1)
        ret5 = _compound_pct([p for p in pcts[-5:] if p is not None])
        f1d = next((x for x in reversed(nets) if x is not None), None)
        rows.append({
            "sector": nm, "kind": "概念",
            "net5": f5d, "penz_seq": _smooth_seq(zfull, 3, 5), "pen_accel": accel,
            "flow_margin": _margin(nets), "ret5": ret5, "n": comp.get(nm, 0),
            "f1d": round(f1d, 1) if f1d is not None else None,
            "net_seq": [round(x, 1) if x is not None else None for x in nets[-5:]],
            "ma5": None,                                            # 概念无成分宽度
            "ambush": bool(f5d > 0 and -3 <= (ret5 or 0) < 3),     # 资金进+价没涨(走平·非大跌)=暗流
        })
    return rows


# ── 概念资金持续流入榜（多窗口变化 + 渗透率·相对强度·点开看成分股）──────────────────────
# 非题材归类池（次新/业绩预告类）：随财报季机械进出·非真炒作热点·从榜单剔除
_NON_THEME = ("次新股", "预增", "预减", "预亏", "预盈", "扭亏", "摘帽", "年报", "季报", "中报", "举牌")


def _is_non_theme(name: str) -> bool:
    """概念名是否为「非题材归类池」（次新股/业绩预告等·非可炒作主题）。"""
    return any(k in name for k in _NON_THEME)


# 「宽概念」分桶：跨行业/风格/宽基/宽泛地域——绝对净额天天占前排却非可操作赛道·打标供前端一键过滤。
# 只标注不删除（用户可切回全看）·且不影响任何资金计算列。分类透明化(告诉用户为何算宽)。
_BROAD_BUCKETS = {
    "持股状态": ("大基金持股", "社保", "QFII", "汇金", "证金", "养老金", "机构重仓", "基金重仓", "险资"),
    "风格因子": ("高股息", "高分红", "破净", "低市盈", "低估值", "绩优", "白马", "蓝筹",
                 "高价股", "低价股", "大盘股", "中盘", "小盘", "微盘"),
    "宽基指数": ("新质", "沪深300", "上证50", "上证180", "深成指", "中证", "MSCI", "富时", "标普",
                 "同花顺"),   # 同花顺中特估100/漂亮100/出海50等自编指数=宽基非赛道
    "央国企": ("国企改革", "央企", "国资改革"),
    "宽泛地域": ("自贸", "一带一路", "京津冀", "长江经济", "西部大开发", "振兴东北", "粤港澳", "海南自贸"),
    "参股类": ("参股",),
}


def _broad_reason(name: str) -> str | None:
    """概念名是否属「宽概念」→ 返回所属类别(持股状态/风格因子/宽基指数/央国企/宽泛地域/参股类)，否则 None。"""
    for label, kws in _BROAD_BUCKETS.items():
        if any(kw in name for kw in kws):
            return label
    return None


def _fetch_concept_member_codes_wide(provider, cap_max: int = 1600) -> "pd.DataFrame":
    """全题材概念成分长表(concept_name/member_code)·成分数∈[5,cap_max]·剔垃圾+非题材池。

    独立于 theme_wide 的 300 上限（不影响热度看板），**覆盖大概念**(人形机器人456/机器人1204)，
    供概念「渗透率」分母（成分流通市值合计）。逐概念 ths_member·较慢·按 ISO 周缓存。
    """
    from app.factors.theme_wide import _is_junk_concept
    pro = provider._ts._api
    try:
        idx = pro.ths_index(type="N")
    except Exception as e:
        logger.warning("[概念渗透率] ths_index 失败: %s", e)
        return pd.DataFrame()
    if idx is None or idx.empty:
        return pd.DataFrame()
    idx = idx.copy()
    idx["count"] = pd.to_numeric(idx.get("count"), errors="coerce")
    idx = idx[(idx["count"] >= 5) & (idx["count"] <= cap_max)]
    idx = idx[~idx["name"].astype(str).apply(lambda s: _is_junk_concept(s) or _is_non_theme(s))]
    rows = []
    for _, r in idx.iterrows():
        name = str(r["name"])
        try:
            m = pro.ths_member(ts_code=r["ts_code"])
        except Exception:
            continue
        if m is None or m.empty or "con_code" not in m.columns:
            continue
        for con in m["con_code"]:
            rows.append({"concept_name": name, "member_code": str(con)})
    logger.info("[概念渗透率] 宽成分缓存：%d 概念 / %d 条", idx.shape[0], len(rows))
    return pd.DataFrame(rows)


def _concept_member_codes_wide(provider) -> dict:
    """{概念名: [成分ts_code]}·按 ISO 周缓存（成分变动慢）。覆盖大概念·供渗透率分母。"""
    import datetime as _dt

    from app.data.cache import cached_daily
    iso = _dt.date.today().isocalendar()
    wk = f"{iso[0]}W{iso[1]:02d}"
    df = cached_daily("concept_members_wide", wk, lambda: _fetch_concept_member_codes_wide(provider))
    if df is None or df.empty:
        return {}
    return {nm: g["member_code"].tolist() for nm, g in df.groupby("concept_name")}


# ── 💱 今日资金切换雷达（增量视角：只报"今天异动"·2026-08-03 用户定档）─────────────────
# 病根同板块诊断页：存量排名表看不出切换。解法：每个概念拿**自己近10日**做基线，
# 只报今天显著偏离基线的（转入侧）+ 主线池里今天掉头的（转出侧）。
# 口径纪律：净额会穿越0·禁用百分比变化——用 中位+k×MAD 的绝对亿元差（稳健·不受离群日污染）。

def _mad_scale(vals: list[float], floor: float = 0.8) -> float:
    """稳健波动尺度：MAD×1.4826，下限 floor 亿（小概念资金流常年≈0·MAD会退化成0→全员误报）。"""
    import numpy as np
    a = np.array(vals, dtype=float)
    mad = float(np.median(np.abs(a - np.median(a)))) * 1.4826
    return max(mad, floor)


def classify_switch_in(adj: list[float | None], raw: list[float | None],
                       k: float = 3.0, min_abs: float = 3.0) -> list[str]:
    """转入判定。adj=横截面去中位后的序列(检验用·切换=相对全市场的异动)，raw=原始净额(标签用)。

    判据(adj)：今日>0 且 ≥ 自身基线中位 + k×MAD 且 ≥ min_abs 亿。
    标签(raw)：首次转正(前3日均≤0) / 创N日新高 / 连2日加速。
    k=3.0/min_abs=3.0 由 20260722-0731 八日回放校准（日均10.6条·横截面去中位前为27.9条——
    普涨日全员"异动"的假阳性被去中位消掉）。
    """
    import numpy as np
    hist = [x for x in adj[:-1] if x is not None]
    today = adj[-1]
    if today is None or len(hist) < 5:
        return []
    if not (today > 0 and today >= float(np.median(hist)) + k * _mad_scale(hist)
            and today >= min_abs):
        return []
    tags = ["异动流入"]
    last3 = [x for x in raw[-4:-1] if x is not None]
    if len(last3) >= 3 and all(x <= 0 for x in last3):
        tags.append("首次转正")
    rhist = [x for x in raw[:-1] if x is not None]
    if raw[-1] is not None and rhist and raw[-1] >= max(rhist):
        tags.append(f"创{len(rhist) + 1}日新高")
    if (raw[-1] is not None and raw[-2] is not None and raw[-3] is not None
            and raw[-1] > raw[-2] > raw[-3] and raw[-2] > 0):
        tags.append("连2日加速")
    return tags


def classify_switch_out(adj: list[float | None], raw: list[float | None],
                        cum10: float = 0.0, k: float = 2.0) -> list[str]:
    """转出判定（只对主线池调用——没进过钱的概念"流出"没有信息量）。

    两条触发路径（满足其一）：
      A 相对异动(adj)：今日 ≤ 自身基线中位 − k×MAD 且 今日<0；
      B 大额流出兜底(raw)：今日流出 ≥ 近10日日均净额的1.5倍（高波动主线的MAD会被撑大·
        路径A漏报——存储芯片单日-179亿曾被标成"歇脚"，由此补路径B）。
    标签(raw)：首日转出 / 连N日流出(N≥2=退潮确认) / 大额流出。
    """
    import numpy as np
    today_a, today_r = adj[-1], raw[-1]
    hist = [x for x in adj[:-1] if x is not None]
    if today_a is None or today_r is None or len(hist) < 5:
        return []
    hit_a = today_a < 0 and today_a <= float(np.median(hist)) - k * _mad_scale(hist)
    daily_avg = cum10 / 10
    hit_b = cum10 > 20 and today_r <= -1.5 * daily_avg
    if not (hit_a or hit_b):
        return []
    n = 0
    for x in reversed(raw):
        if x is None or x >= 0:
            break
        n += 1
    tags = ["首日转出"] if n <= 1 else [f"连{n}日流出"]
    if hit_b:
        tags.append("大额流出")
    return tags


def _group_by_overlap(rows: list[dict], mmap: dict, th: float = 0.4) -> list[dict]:
    """成分重叠聚族：机器人/减速器/人形机器人同日齐触发会刷屏——一族只留一行。

    重叠系数=|A∩B|/min(|A|,|B|)≥th 视为同族；代表=族内今日净额最大者，其余进 kin 列表。
    mmap 缺失(周缓存未建)→不分组原样返回（宁可多显示·不静默吞）。
    """
    if not mmap or len(rows) < 2:
        return rows
    sets = {r["concept"]: set(mmap.get(r["concept"]) or []) for r in rows}
    used, out = set(), []
    for r in rows:                                     # rows 已按今日净额降序→先到先当代表
        nm = r["concept"]
        if nm in used:
            continue
        kin = []
        for r2 in rows:
            n2 = r2["concept"]
            if n2 == nm or n2 in used:
                continue
            a, b = sets[nm], sets[n2]
            if a and b and len(a & b) / min(len(a), len(b)) >= th:
                kin.append(n2)
                used.add(n2)
        used.add(nm)
        r = dict(r)
        r["kin"], r["kin_n"] = kin[:8], len(kin)
        out.append(r)
    return out


def _concept_structure_map(date: str) -> dict:
    """读板块诊断页已建好的概念结构缓存（sector_mtf/concept_{date}_v2.json）→ {概念名: 结构摘要}。

    只读文件不触发重建（重建要几分钟·雷达必须秒出）；缓存不存在→回退空(标签留空·不编)。
    """
    import json

    from app.config import get_settings
    f = get_settings().cache_dir / "sector_mtf" / f"concept_{date}_v2.json"
    if not f.exists():
        return {}
    try:
        rows = json.loads(f.read_text(encoding="utf-8")).get("rows", [])
        return {r["sector"]: {"monthly_dir": r.get("monthly_dir"),
                              "m_pattern": r.get("m_pattern"), "w_event": r.get("w_event")}
                for r in rows}
    except Exception:
        return {}


def build_concept_switch_radar(date: str, window: int = 11, provider=None) -> dict:
    """💱 今日资金切换雷达：转入(相对异动) + 转出(主线池掉头) + 🚦主线健康灯。

    数据全部来自已按日缓存的 ths_concept_flow（零新增接口）；结构标签读概念月线缓存；
    同族概念(成分重叠)聚合为一行。描述档·同花顺DDE估算口径·非买卖建议。
    """
    import numpy as np
    provider = provider or CompositeProvider()
    pro = provider._ts._api
    from app.data.cache import cached_daily
    from app.factors.theme_wide import _is_junk_concept
    dates = _recent_trade_dates(provider, date, window)
    if len(dates) < 6:
        raise ValueError(f"{date} 可用交易日不足({len(dates)})")

    net_by, lead, comp = [], {}, {}
    for d in dates:
        df = cached_daily("ths_concept_flow", d, lambda d=d: _fetch_concept_flow(pro, d))
        m = {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                nm = str(r.get("name", "") or "")
                # 雷达池：剔垃圾/非题材/宽概念——切换要看"可操作赛道"，宽概念天天霸榜全是噪声
                if not nm or _is_junk_concept(nm) or _is_non_theme(nm) or _broad_reason(nm):
                    continue
                na = r.get("net_amount")
                m[nm] = float(na) if pd.notna(na) else None
                if d == dates[-1]:
                    if r.get("lead_stock"):
                        lead[nm] = str(r["lead_stock"])
                    if pd.notna(r.get("company_num")):
                        comp[nm] = int(r["company_num"])
        net_by.append(m)
    if not net_by[-1]:
        raise ValueError(f"{date} 当日概念资金流为空（未入库或非交易日）")

    # 横截面去中位：adj = net − 当日全概念中位（普涨日全市场齐进钱≠切换·切换是相对异动）
    meds = [float(np.median([v for v in m.values() if v is not None])) if m else 0.0
            for m in net_by]
    struct = _concept_structure_map(dates[-1])
    names = [nm for nm in net_by[-1] if comp.get(nm, 0) >= 5]
    raw = {nm: [m.get(nm) for m in net_by] for nm in names}
    adj = {nm: [(m.get(nm) - md) if m.get(nm) is not None else None
                for m, md in zip(net_by, meds)] for nm in names}
    cum10 = {nm: sum(x for x in s[:-1][-10:] if x is not None) for nm, s in raw.items()}
    mainline = sorted(cum10, key=lambda n: -cum10[n])[:40]        # 主线池=近10日(不含今日)累计前40

    def _row(nm: str, tags: list[str]) -> dict:
        s = raw[nm]
        hist = [x for x in s[:-1] if x is not None]
        st = struct.get(nm) or {}
        return {
            "concept": nm, "tags": tags,
            "today": round(s[-1], 1), "base_med": round(float(np.median(hist)), 1),
            "cum10": round(cum10.get(nm, 0.0), 1),
            "seq5": [round(x, 1) if x is not None else None for x in s[-5:]],
            "lead": lead.get(nm, ""), "n": comp.get(nm, 0),
            "monthly_dir": st.get("monthly_dir"), "m_pattern": st.get("m_pattern"),
            "w_event": st.get("w_event"),
        }

    flow_in = [_row(nm, t) for nm in names if (t := classify_switch_in(adj[nm], raw[nm]))]
    flow_in.sort(key=lambda r: -r["today"])
    flow_out = [_row(nm, t) for nm in mainline
                if (t := classify_switch_out(adj[nm], raw[nm], cum10.get(nm, 0.0)))]
    flow_out.sort(key=lambda r: r["today"])
    mmap = _concept_member_codes_wide(provider)                    # 周缓存·仅读
    flow_in = _group_by_overlap(flow_in, mmap)
    flow_out = _group_by_overlap(flow_out, mmap)

    lamps = []
    for nm in mainline[:15]:                                       # 🚦主线健康灯=池前15
        s = raw[nm]
        out_tags = classify_switch_out(adj[nm], s, cum10.get(nm, 0.0))
        t0 = s[-1] or 0
        state = (out_tags[0] if out_tags
                 else "吸金中" if t0 > 0
                 else "歇脚" if t0 > -max(1.0, cum10[nm] / 20) else "流出中")
        lamps.append({"concept": nm, "cum10": round(cum10[nm], 1),
                      "today": round(s[-1], 1) if s[-1] is not None else None,
                      "state": state, "monthly_dir": (struct.get(nm) or {}).get("monthly_dir")})

    return {
        "date": dates[-1], "in": flow_in[:12], "out": flow_out[:12], "mainline": lamps,
        "n_pool": len(names),
        "note": ("转入=今日净额相对全市场(横截面去中位)显著高于自身近10日基线(中位+3×MAD)；"
                 "转出只盯主线池(近10日累计前40·中位−2×MAD且为负)。已剔宽概念/业绩预告池·"
                 "成分重叠概念聚为一族。同花顺DDE估算·非龙虎榜真钱·描述档未回测·非买卖建议。"),
    }


def build_concept_persistent_flow(date: str, window: int = 10, provider=None) -> dict:
    """概念「资金持续流入榜」：近 window 日同花顺概念净流入 + **渗透率(净流入/概念流通市值·相对强度)**
    + 多窗口(今/1日变化/近3/3日变化/近5/近10) + 连续流入天。渗透率抓"小盘子资金猛灌"的真热点。

    ⚠️口径：同花顺概念·**成分严重重叠**→概念流通市值重复计数·**渗透率是近似**(行业口径更干净)；净流入非龙虎榜真钱。
    """
    import math

    import pandas as pd
    from app.data.cache import cached_daily
    from app.factors.theme_wide import _is_junk_concept, concept_members_map
    from app.strategy.industry_flow import _series_metrics
    provider = provider or CompositeProvider()
    pro = provider._ts._api
    dates = _recent_trade_dates(provider, date, window)
    if not dates:
        raise ValueError(f"{date} 无交易日")

    def _num(x):
        """转 float；None/NaN/±inf → None（防脏值污染累计与渗透率）。"""
        if x is None or (isinstance(x, float) and not math.isfinite(x)):
            return None
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    net_by, pct_by, comp, lead = [], [], {}, {}
    for d in dates:
        df = cached_daily("ths_concept_flow", d, lambda d=d: _fetch_concept_flow(pro, d))
        nm_net, nm_pct = {}, {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                nm = str(r.get("name", "") or "")
                if not nm or _is_junk_concept(nm) or _is_non_theme(nm):     # 剔垃圾 + 非题材归类池
                    continue
                nm_net[nm] = _num(r.get("net_amount"))
                nm_pct[nm] = _num(r.get("pct_change"))
                cn = _num(r.get("company_num"))
                if cn is not None:
                    comp[nm] = int(cn)
                if r.get("lead_stock"):
                    lead[nm] = str(r["lead_stock"])
        net_by.append(nm_net)
        pct_by.append(nm_pct)

    # 概念流通市值(渗透率分母·end日·成分circ_mv合计)：宽 map 覆盖大概念·回退窄 map
    mmap = _concept_member_codes_wide(provider) or concept_members_map(provider)
    db = provider.get_daily_basic(dates[-1])
    circ = (pd.to_numeric(db.set_index("ts_code")["circ_mv"], errors="coerce") / 1e4
            if db is not None else pd.Series(dtype=float))
    concept_circ = {nm: float(circ.reindex(codes).dropna().sum()) for nm, codes in mmap.items()}

    names = set().union(*[set(m) for m in net_by]) if net_by else set()
    rows = []
    for nm in names:
        if comp.get(nm, 0) < 5:
            continue
        nets = [m.get(nm) for m in net_by]
        if sum(1 for x in nets if x is not None) < max(5, int(window * 0.5)):
            continue
        pcts = [m.get(nm) for m in pct_by]
        met = _series_metrics(nets, pcts)                          # 复用行业多窗口指标(cum3/5/10·delta1d/3d·consec…)
        cc = concept_circ.get(nm)
        c5 = _num(met.get("cum5"))
        pen5 = (round(c5 / cc * 100, 3)
                if cc and math.isfinite(cc) and cc > 0 and c5 is not None else None)  # 渗透率%(相对强度)
        met.update({
            "concept": nm, "n": comp.get(nm, 0), "lead": lead.get(nm, ""),
            "circ": round(cc, 0) if cc and math.isfinite(cc) and cc > 0 else None,
            "pen5": pen5,
        })
        rows.append(met)
    df_out = pd.DataFrame(rows)
    if df_out.empty:
        return {"date": date, "window": len(dates), "rows": []}
    df_out = df_out.sort_values("cum5", ascending=False).reset_index(drop=True)
    df_out["rank"] = df_out.index + 1
    records = df_out.to_dict("records")
    struct = _concept_structure_map(dates[-1])   # 月线方向标签(读缓存·资金×结构交叉印证)
    for rec in records:   # 宽概念标注在 to_dict 之后赋值·绕开 pandas 把 None 变 NaN(保证前端拿到 null 而非 NaN·不影响任何资金列)
        rec["broad"] = _broad_reason(str(rec.get("concept", "")))
        rec["monthly_dir"] = (struct.get(str(rec.get("concept", ""))) or {}).get("monthly_dir")
    return {
        "date": date, "window": len(dates),
        "dates": [f"{d[4:6]}-{d[6:]}" for d in dates],
        "rows": records,
        "note": ("同花顺概念净流入(DDE·非龙虎榜真钱)。**渗透率%=近5日净流入/概念流通市值**(相对强度·抓小盘子猛灌)。"
                 "⚠️概念成分重叠·流通市值重复计数·渗透率近似。红=流入 绿=流出。"
                 " · 「宽」=跨行业/风格/宽基/宽泛地域概念(持股状态/高股息/新质50/自贸区等)·非可操作赛道·可一键隐藏突出真题材。"),
    }
