"""多维资金全正 · 择时分档（借鉴吴川框架 · 诚实校准）。

对每个板块（申万二级行业 / 同花顺概念）盘后计算：
- **6 窗口主力净流入累计**（60/20/10/5/3/1 日）+ **全正维数** + **洗盘/撤退/健康** 判别
  （中长线正 + 短期负 = 洗盘；中长线也转负 = 撤退；六维全正 = 健康）
- **板块指数 MA20 乖离 → 低吸/蓄势/强势/过热** 档（资金给方向 · 乖离给时机）
- **龙头**（板块内当日主力净流入最大） + **渗透率**（净流入/板块体量 · 相对强度）

⚠️ 诚实纪律：主力净流入=估算（超大单+大单 · 东财口径 · **非龙虎榜真钱**）；资金作为"预测器"实测
**H10 IC≈0** → 本表是"**看清资金结构 + 用乖离择时**"工具，**非预测/胜率/必涨**；真正有 edge 的是乖离/低吸位。
吴川未做前向验证 · 本表分档阈值为专家先验（可后续用事件研究校准）。JSON 日缓存 · 供 /fundalign 秒开。
"""

from __future__ import annotations

import json
import logging

import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)


def _trade_dates(prov: CompositeProvider, end: str, n: int) -> list[str]:
    """含 end 在内的最近 n 个交易日（升序）。自算日历窗口（_recent_trade_dates 只回~17日·不够60窗口）。"""
    import datetime
    start = (datetime.datetime.strptime(end, "%Y%m%d") - datetime.timedelta(days=int(n * 1.7) + 20)).strftime("%Y%m%d")
    cal = prov.get_trade_cal(start, end)
    if cal is None or cal.empty:
        return []
    days = sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist())
    return days[-n:]

_WINDOWS = (60, 20, 10, 5, 3, 1)          # 吴川口径：长→短
_LOOKBACK = 62                            # 拉 62 交易日够 60 窗口
# MA20 乖离分档阈值（专家先验·板块指数(close-MA20)/MA20 %·实测弱市多在-6~+12·可校准）
_BIAS_TIERS = ((5.0, "低吸"), (15.0, "蓄势"), (35.0, "强势"), (1e9, "过热"))


def _tier(bias: float | None) -> str:
    if bias is None:
        return "—"
    if bias < -3:
        return "破位"                       # 跌破 MA20·非低吸机会·是走坏
    for thr, name in _BIAS_TIERS:
        if bias <= thr:
            return name
    return "过热"


# ── 每个板块的 60 日主力净流入序列 ───────────────────────────────────────────
def _industry_net_matrix(dates: list[str], prov: CompositeProvider) -> dict[str, list]:
    """申万二级：逐日 _industry_agg（elg+lg 东财口径）→ {行业: [按 dates 顺序的日净流入(亿)]}。"""
    from app.strategy.industry_flow import _industry_agg
    sb = prov.get_stock_basic()
    c2n = dict(zip(sb["ts_code"], sb["name"]))
    c2i = dict(zip(sb["ts_code"], sb["industry"])) if "industry" in sb.columns else {}
    per_day = []
    for d in dates:
        agg = _industry_agg(d, prov, c2n, c2i)
        per_day.append(dict(zip(agg["industry"], agg["main_flow"])) if not agg.empty else {})
    boards = set().union(*[set(m) for m in per_day]) if per_day else set()
    return {b: [m.get(b) for m in per_day] for b in boards}


def _concept_net_matrix(dates: list[str], prov: CompositeProvider) -> dict[str, list]:
    """同花顺概念：逐日 moneyflow_cnt_ths 净额(亿) → {概念: [日净额]}。剔垃圾/非题材概念。"""
    from app.data.cache import cached_daily
    from app.factors.theme_wide import _is_junk_concept
    from app.strategy.concept_flow import _fetch_concept_flow, _is_non_theme
    pro = prov._ts._api
    per_day = []
    for d in dates:
        df = cached_daily("ths_concept_flow", d, lambda d=d: _fetch_concept_flow(pro, d))
        m = {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                nm = str(r.get("name", "") or "")
                if nm and not _is_junk_concept(nm) and not _is_non_theme(nm):
                    try:
                        m[nm] = float(r.get("net_amount"))
                    except (TypeError, ValueError):
                        pass
        per_day.append(m)
    boards = set().union(*[set(m) for m in per_day]) if per_day else set()
    return {b: [m.get(b) for m in per_day] for b in boards}


# ── 多窗口累计 + 全正维数 + 洗盘/撤退 ─────────────────────────────────────────
def _multi_window(series: list) -> dict:
    """series=按日期升序的日净流入(亿·None=缺)。算 6 窗口累计 + 全正维数 + 洗盘/撤退标签。"""
    def cum(n: int) -> float | None:
        vals = [v for v in series[-n:] if v is not None]
        return round(sum(vals), 2) if vals else None

    cums = {w: cum(w) for w in _WINDOWS}
    pos = sum(1 for w in _WINDOWS if (cums[w] or 0) > 0)          # 全正维数(0-6)
    mid_ok = all((cums[w] or 0) > 0 for w in (60, 20, 10, 5))     # 中长线四维全正
    short_neg = (cums[3] or 0) <= 0 or (cums[1] or 0) <= 0        # 短期(3或1日)转负
    c5, c60 = cums[5] or 0, cums[60] or 0
    if pos == 6:
        state = "健康"                                            # 六维全正·一致流入
    elif mid_ok and short_neg:
        state = "洗盘"                                            # 中长四维全正+短期负=洗盘(资金没走)
    elif c5 <= 0:
        state = "撤退"                                            # 近5日在流出(短期资金真走)
    elif c60 <= 0 and c5 > 0:
        state = "修复"                                            # 长期负但近5日转正=底部回流
    else:
        state = "分歧"
    return {"cum": cums, "pos_dims": pos, "state": state}


# ── 板块指数 MA20 乖离（择时档）──────────────────────────────────────────────
def _board_bias(kind: str, name: str, code_map: dict, end: str, prov: CompositeProvider) -> float | None:
    """板块指数 (收盘−MA20)/MA20 %。用 sector_mtf._index_daily（干净板块指数·非成分合成）。"""
    from app.strategy.sector_mtf import _index_daily
    code = code_map.get(name)
    if not code:
        return None
    try:
        k = _index_daily(prov, kind, code, end)
        if k is None or k.empty or len(k) < 20:
            return None
        close = pd.to_numeric(k["close"], errors="coerce")
        ma20 = close.rolling(20).mean().iloc[-1]
        if not ma20 or pd.isna(ma20):
            return None
        return round((float(close.iloc[-1]) / float(ma20) - 1) * 100, 1)
    except Exception as e:
        logger.debug("[资金全正] %s 乖离失败: %s", name, e)
        return None


# ── 龙头（板块内当日主力净流入最大）─────────────────────────────────────────
def _board_leaders(date: str, kind: str, prov: CompositeProvider) -> dict[str, dict]:
    """{板块: {龙头name, 龙头净流入(亿)}}·当日 elg+lg 口径。"""
    from app.data.moneyflow import main_net_wan
    mf = prov.get_money_flow(date)
    net = main_net_wan(mf) / 1e4                                  # 亿·index=ts_code
    sb = prov.get_stock_basic()
    name_map = dict(zip(sb["ts_code"].astype(str), sb["name"].astype(str)))
    out: dict[str, dict] = {}
    if kind == "industry":
        c2i = dict(zip(sb["ts_code"].astype(str), sb["industry"].astype(str)))
        by: dict[str, tuple] = {}
        for ts, v in net.items():
            b = c2i.get(ts)
            if b and (b not in by or v > by[b][1]):
                by[b] = (ts, float(v))
        out = {b: {"name": name_map.get(ts, ts[:6]), "net_yi": round(v, 2)} for b, (ts, v) in by.items()}
    else:                                                        # concept：用成分映射
        from app.factors.theme_wide import concept_members_map
        mmap = concept_members_map(prov)
        for nm, codes in mmap.items():
            best, bestv = None, -1e18
            for c in codes:
                v = float(net.get(c, -1e18))
                if v > bestv:
                    best, bestv = c, v
            if best is not None and bestv > -1e17:
                out[nm] = {"name": name_map.get(best, best[:6]), "net_yi": round(bestv, 2)}
    return out


# ── 主构建 ───────────────────────────────────────────────────────────────────
def build_fund_alignment(date: str, kind: str = "industry", force: bool = False,
                         provider: CompositeProvider | None = None) -> dict:
    """多维资金全正·择时分档表。kind: industry(申万二级·elg+lg东财口径) / concept(同花顺)。JSON日缓存。"""
    cdir = get_settings().cache_dir / "fund_alignment"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{date}_{kind}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    prov = provider or CompositeProvider()
    dates = _trade_dates(prov, date, _LOOKBACK)
    if len(dates) < 20:
        return {"ok": False, "date": date, "kind": kind, "msg": "交易日不足(需≈60日)"}

    matrix = (_industry_net_matrix(dates, prov) if kind == "industry"
              else _concept_net_matrix(dates, prov))
    # 板块指数代码映射（乖离用）
    from app.strategy.sector_mtf import _concept_code_map, _sw_code_map
    code_map = _sw_code_map(prov) if kind == "industry" else _concept_code_map(prov, date)
    leaders = _board_leaders(date, kind, prov)

    rows = []
    for board, series in matrix.items():
        if sum(1 for v in series if v is not None) < 5:          # 数据太少跳过
            continue
        mw = _multi_window(series)
        if mw["pos_dims"] < 3:                                   # 只留有一定持续性的(≥3维正)·省乖离计算
            continue
        bias = _board_bias(kind, board, code_map, date, prov)
        lead = leaders.get(board, {})
        rows.append({
            "board": board, "cum": mw["cum"], "pos_dims": mw["pos_dims"], "state": mw["state"],
            "bias": bias, "tier": _tier(bias),
            "leader": lead.get("name", ""), "leader_net_yi": lead.get("net_yi"),
        })
    # 排序：全正维数 → 60日累计（中长线强度）
    rows.sort(key=lambda r: (r["pos_dims"], r["cum"].get(60) or 0), reverse=True)

    out = {
        "ok": True, "date": date, "kind": kind,
        "windows": list(_WINDOWS),
        "counts": {
            "six": sum(1 for r in rows if r["pos_dims"] == 6),
            "four_mid": sum(1 for r in rows if all((r["cum"].get(w) or 0) > 0 for w in (60, 20, 10, 5))),
            "washout": sum(1 for r in rows if r["state"] == "洗盘"),
            "retreat": sum(1 for r in rows if r["state"] == "撤退"),
        },
        "rows": rows,
        "note": ("主力净流入=估算(超大单+大单·东财口径·非真钱)；资金作预测器实测H10 IC≈0→本表看清资金结构+乖离择时·"
                 "非预测/胜率；乖离/低吸位才更有edge。分档阈值为专家先验。盘后EOD·非买卖建议。"),
    }
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out
