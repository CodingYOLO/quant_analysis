"""
板块大周期方向榜：行业(申万二级)/概念(同花顺) 的 **月线定方向 · 周线定节奏**（盘后·日缓存）。

对标"大周期决定方向·小周期决定节奏"——先看板块月线是否主升浪/见顶，再落到日线资金/个股。
数据源（板块指数日线·干净OHLC·非成分合成）：
  - 行业：申万二级指数 `sw_daily(801xxx.SI)`（index_classify 映射 申万二级名→指数代码）。
  - 概念：同花顺概念指数 `ths_daily(886xxx.TI)`（moneyflow_cnt_ths 映射 概念名→指数代码）。
判定复用 stock_profile._mtf_analysis（月线10月线方向 + 见顶三条件 + 周线节奏），口径与个股一致。
"""

from __future__ import annotations

import datetime
import logging

import pandas as pd

from app.data.cache import cached_daily
from app.data.composite_provider import CompositeProvider
from app.strategy.stock_profile import _mtf_analysis, _resample_ohlc, _kline_payload

logger = logging.getLogger(__name__)

_MIN_BARS = 300          # 板块指数至少 300 日(≈1.3年)才够月线判定


# ── 板块名 → 指数代码 映射（周缓存·变动慢）───────────────────────────────────
def _sw_code_map(provider: CompositeProvider) -> dict:
    """申万二级行业名 → 指数代码(801xxx.SI)。"""
    def _fetch():
        ic = provider._ts._api.index_classify(level="L2", src="SW2021")
        if ic is None or ic.empty:
            return pd.DataFrame()
        return ic[["index_code", "industry_name"]]
    iso = datetime.date.today().isocalendar()
    df = cached_daily("sw_l2_classify", f"{iso[0]}W{iso[1]:02d}", _fetch)
    if df is None or df.empty:
        return {}
    return dict(zip(df["industry_name"].astype(str), df["index_code"].astype(str)))


def _concept_code_map(provider: CompositeProvider, end: str) -> dict:
    """同花顺概念名 → 概念指数代码(886xxx.TI)。取自当日 moneyflow_cnt_ths（含 name/ts_code）。"""
    def _fetch():
        df = provider._ts._api.moneyflow_cnt_ths(trade_date=end)
        if df is None or df.empty:
            return pd.DataFrame()
        return df[["name", "ts_code"]]
    df = cached_daily("ths_concept_codes", end, _fetch)
    if df is None or df.empty:
        return {}
    return dict(zip(df["name"].astype(str), df["ts_code"].astype(str)))


# ── 板块指数日线（日缓存）────────────────────────────────────────────────────
def _index_daily(provider: CompositeProvider, kind: str, code: str, end: str) -> pd.DataFrame:
    """板块指数近 ~2.5 年日线(OHLCV·升序)。行业=sw_daily·概念=ths_daily。空→空表。"""
    start = (datetime.date.today() - datetime.timedelta(days=1000)).strftime("%Y%m%d")

    def _fetch():
        pro = provider._ts._api
        fn = pro.sw_daily if kind == "industry" else pro.ths_daily
        try:
            df = fn(ts_code=code, start_date=start, end_date=end)
        except Exception as e:
            logger.debug("[板块大周期] %s 指数拉取失败: %s", code, e)
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date").reset_index(drop=True)
        keep = [c for c in ("trade_date", "open", "high", "low", "close", "vol") if c in df.columns]
        if "vol" not in df.columns and "amount" in df.columns:      # 概念指数无 vol → 用成交额代量
            df["vol"] = pd.to_numeric(df["amount"], errors="coerce")
            keep.append("vol")
        return df[keep]
    return cached_daily(f"sector_idx_{kind}", f"{code}_{end}", _fetch)


def _row(name: str, kind: str, k: pd.DataFrame) -> dict | None:
    """单板块大周期行：月线方向 + 见顶N/3 + 周线节奏 + 偏离10月线%。"""
    if k is None or len(k) < _MIN_BARS or "vol" not in k.columns:
        return None
    mtf = _mtf_analysis(k)
    mo, wk = mtf.get("monthly", {}), mtf.get("weekly", {})
    if not mo:
        return None
    dev = (round((mo["close"] / mo["ma10"] - 1) * 100, 1)
           if mo.get("close") and mo.get("ma10") else None)
    return {
        "sector": name, "kind": kind,
        "monthly_dir": mo.get("dir"), "top_count": mo.get("top_count", 0),
        "ma10_up": mo.get("ma10_up"), "above_ma10": mo.get("above_ma10"),
        "weekly_rhythm": wk.get("rhythm"), "dev_ma10": dev,
    }


# ── 板块大周期榜（日缓存）────────────────────────────────────────────────────
_DIR_ORDER = {"主升浪": 0, "月线向上": 1, "月线震荡": 2, "月线见顶": 3, "月线走坏": 4}


def _dir_rank(d: str) -> int:
    for key, r in _DIR_ORDER.items():
        if key in (d or ""):
            return r
    return 5


def build_sector_mtf(end: str, kind: str = "industry", provider: CompositeProvider | None = None,
                     force: bool = False) -> dict:
    """构建板块大周期方向榜（月线定方向·周线定节奏）。kind: industry(申万二级) / concept(同花顺)。日缓存(JSON)。"""
    import json

    from app.config import get_settings
    cdir = get_settings().cache_dir / "sector_mtf"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{kind}_{end}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    prov = provider or CompositeProvider()
    code_map = _sw_code_map(prov) if kind == "industry" else _concept_code_map(prov, end)
    rows = []
    for name, code in code_map.items():
        r = _row(name, kind, _index_daily(prov, kind, code, end))
        if r:
            rows.append(r)
    rows.sort(key=lambda r: (_dir_rank(r["monthly_dir"]), -(r["dev_ma10"] or -999)))
    out = {
        "ok": True, "end": end, "kind": kind, "n": len(rows), "rows": rows,
        "note": ("板块指数月线/周线（行业=申万二级指数·概念=同花顺概念指数）。月线定方向(10月线+见顶三条件)、"
                 "周线定节奏。顺大势逆小势：月线向上+周线回踩=低吸猎场；月线见顶三条件≥2共振才是真离场。"
                 "盘后更新·纯结构描述·非买卖建议。红涨绿跌。"),
    }
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out


def _is_up(r: dict) -> bool:
    return bool(r.get("monthly_dir") and any(k in r["monthly_dir"] for k in ("主升浪", "向上", "健康")))


def _is_dip(r: dict) -> bool:
    """低吸猎场：月线向上 + 周线回踩 + 无见顶 + 未过度偏离(≤35%)。与前端 isDip 一致。"""
    return bool(_is_up(r) and "回踩" in (r.get("weekly_rhythm") or "")
                and (r.get("top_count") or 0) == 0
                and (r.get("dev_ma10") is None or r["dev_ma10"] <= 35))


def _mtf_lines(rows: list[dict], n: int = 14) -> str:
    out = []
    for r in rows[:n]:
        rh = (r.get("weekly_rhythm") or "").split("·")[0]
        out.append(f"- {r['sector']}（偏离10月线{r.get('dev_ma10')}%·{rh}·见顶{r.get('top_count', 0)}/3）")
    return "\n".join(out) or "（无）"


def build_sector_mtf_ai(end: str, provider: CompositeProvider | None = None, force: bool = False) -> dict:
    """板块大周期格局 AI 研判（读行业+概念大周期榜·LLM综合·日缓存·非买卖建议）。"""
    import json

    from app.config import get_settings
    cdir = get_settings().cache_dir / "sector_mtf"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"ai_{end}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    prov = provider or CompositeProvider()
    ind = build_sector_mtf(end, "industry", prov).get("rows", [])
    con = build_sector_mtf(end, "concept", prov).get("rows", [])
    ind_up = [r for r in ind if _is_up(r)]
    con_up = [r for r in con if _is_up(r)]
    top = sorted([r for r in ind + con if (r.get("top_count") or 0) >= 2], key=lambda r: -(r.get("top_count") or 0))
    dip = [r for r in ind + con if _is_dip(r)]
    data = (f"【行业·月线主升浪/向上（{len(ind_up)}个·偏离大=高位）】\n{_mtf_lines(ind_up)}\n\n"
            f"【概念·月线主升浪/向上（{len(con_up)}个）】\n{_mtf_lines(con_up)}\n\n"
            f"【月线见顶预警（三条件≥2共振）】\n{_mtf_lines(top, 10)}\n\n"
            f"【低吸猎场（月线向上+周线回踩+无见顶+未过度偏离）】\n{_mtf_lines(dip, 12)}")

    prompt = ("你是A股策略研究员，做**板块大周期结构研判**(客观·非荐股·非投资建议)。下方是全市场板块的月线/周线结构"
              "(行业=申万二级指数·概念=同花顺概念指数·偏离10月线大=强势但高位)。请用 160-260 字总结当前**大周期格局**：\n"
              "① 主线方向——哪些板块/产业链在月线主升浪(大周期向上)，是集中在某条链(如半导体)还是分散；\n"
              "② 低吸猎场——月线向上但周线回踩的板块(顺大势逆小势的低吸窗口)，无则如实说明；\n"
              "③ 见顶预警——月线见顶三条件共振的板块，需警惕；\n"
              "④ 一句操作节奏——顺大势逆小势(主升浪偏离大的控仓·回踩的低吸·见顶的回避)。\n"
              "**只依据下方数据·不编造板块名/数字**；这是结构描述与节奏研判、不是买卖建议、不预测涨跌。\n\n" + data)
    try:
        from app.llm.client import LLMClient
        from app.llm.stance import ANALYST_STANCE
        raw = LLMClient().chat([{"role": "user", "content": ANALYST_STANCE + "\n\n" + prompt}],
                               task_type="pro", temperature=0.3, max_tokens=1500)
    except Exception as e:
        logger.warning("[大周期研判] LLM 失败: %s", e)
        raw = ""
    out = {
        "ok": bool(raw), "end": end, "summary": (raw or "").strip(),
        "counts": {"ind_up": len(ind_up), "con_up": len(con_up), "top": len(top), "dip": len(dip)},
        "disclaimer": "AI 基于板块月线/周线结构数据综合·结构描述与节奏研判·非买卖建议·不预测涨跌。",
    }
    if out["ok"]:
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out


def sector_mtf_kline(kind: str, name: str, end: str, provider: CompositeProvider | None = None) -> dict:
    """单板块的 月线/周线 K线 payload（点开展开用）。"""
    prov = provider or CompositeProvider()
    code_map = _sw_code_map(prov) if kind == "industry" else _concept_code_map(prov, end)
    code = code_map.get(name)
    if not code:
        return {"ok": False, "msg": f"未找到板块「{name}」指数代码"}
    k = _index_daily(prov, kind, code, end)
    if k is None or len(k) < _MIN_BARS or "vol" not in k.columns:
        return {"ok": False, "msg": f"「{name}」指数数据不足"}
    return {
        "ok": True, "name": name, "kind": kind,
        "kline_w": _kline_payload(_resample_ohlc(k, "W-FRI").tail(120)),
        "kline_m": _kline_payload(_resample_ohlc(k, "ME").tail(60)),
        "mtf": _mtf_analysis(k),
    }
