"""
主题生命周期归因（诚实版·对标吴川"一日游 vs 持续型"但去掉小样本坑）。

核心问题（波段视角）：一个主题**点火**（板块指数放量领涨）之后，还追得起吗？
是"一日游"（点火当天冲完就还回去）还是"持续型"（越走越强）？

方法（全部用板块指数历史·几年·不依赖我们自己的短选股史·不用 LLM 盯盘次数）：
  1. 点火日：板块指数当日绝对涨幅 ≥ IGN_ABS 且 相对上证超额 ≥ IGN_EXCESS，近 COOLDOWN 日去重。
  2. 前瞻超额：从点火日**收盘**起，到 T+1/3/5/10/20 的**累计超额收益**（板块 − 上证）。
     —— 用超额（扣大盘），不用裸涨幅；用点火收盘入场（现实：看到冲高还能不能追）。
  3. 每个主题聚合历史所有点火事件 → 各期中位超额 + 事件数 → 打标签。

诚实护栏：超额口径 / 样本门槛硬卡(n<MIN_EVENTS 标"样本不足"不排序) / 不按胜率排名 /
框"历史结构描述·非预测·主题成分会变"。阈值由数据定（见 build 里的事件数校验）。
"""

from __future__ import annotations

import json
import logging
from statistics import median

import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.strategy.sector_mtf import _concept_code_map, _index_daily, _sw_code_map

logger = logging.getLogger(__name__)

# ── 可调参数（阈值由数据定·改动记得重跑校验事件数分布）───────────────────────
IGN_ABS = 3.0            # 点火日：板块指数当日绝对涨幅下限(%)
IGN_EXCESS = 1.5         # 点火日：相对上证单日超额下限(百分点)
COOLDOWN = 5             # 点火去重：近 N 日已点火则不重复计（每波只算一次启动）
HORIZONS = (1, 3, 5, 10, 20)   # 前瞻期
MIN_BARS = 250           # 板块指数至少 N 日历史才纳入（样本可靠）
MIN_EVENTS = 8           # 点火事件 < N → 标"样本不足"·仅聚合不单独结论
_BENCH = "000001.SH"     # 基准：上证指数


# ── 基准（上证）收盘序列 ─────────────────────────────────────────────────────
def _benchmark_close(provider: CompositeProvider, start: str, end: str) -> pd.Series:
    """上证收盘·index=trade_date(YYYYMMDD字符串)·升序。空→空 Series。"""
    idx = provider.get_index_daily_range(_BENCH, start, end)
    if idx is None or idx.empty:
        return pd.Series(dtype=float)
    idx = idx.sort_values("trade_date")
    return pd.Series(pd.to_numeric(idx["close"], errors="coerce").values,
                     index=idx["trade_date"].astype(str))


# ── 点火日识别 ───────────────────────────────────────────────────────────────
def _find_ignitions(theme_close: pd.Series, bench_close: pd.Series) -> list[int]:
    """返回点火日在 theme_close 中的**位置索引**列表（已按 COOLDOWN 去重）。"""
    t_ret = theme_close.pct_change() * 100
    b_ret = bench_close.reindex(theme_close.index).pct_change() * 100
    excess = t_ret - b_ret
    hot = (t_ret >= IGN_ABS) & (excess >= IGN_EXCESS)
    out: list[int] = []
    for i, is_hot in enumerate(hot.values):
        if is_hot and (not out or i - out[-1] >= COOLDOWN):
            out.append(i)
    return out


# ── 单事件前瞻累计超额 ───────────────────────────────────────────────────────
def _forward_excess(theme_close: pd.Series, bench_close: pd.Series,
                    i0: int) -> dict[int, float]:
    """从位置 i0(点火日收盘)起·各期累计超额收益(板块−上证·百分点)。越界的期跳过。"""
    b = bench_close.reindex(theme_close.index)
    tc0, bc0 = theme_close.iloc[i0], b.iloc[i0]
    out: dict[int, float] = {}
    if not (tc0 and bc0 and pd.notna(tc0) and pd.notna(bc0)):
        return out
    for h in HORIZONS:
        j = i0 + h
        if j >= len(theme_close):
            continue
        tcj, bcj = theme_close.iloc[j], b.iloc[j]
        if pd.notna(tcj) and pd.notna(bcj) and bcj:
            t_cum = (tcj / tc0 - 1) * 100
            b_cum = (bcj / bc0 - 1) * 100
            out[h] = round(t_cum - b_cum, 2)
    return out


# ── 打标签 ───────────────────────────────────────────────────────────────────
def _classify(n: int, med: dict[int, float]) -> str:
    """由各期中位超额判类型。样本不足优先。阈值宽松·仅作结构描述非预测。"""
    if n < MIN_EVENTS:
        return "样本不足"
    m1, m5, m10 = med.get(1), med.get(5), med.get(10)
    if m5 is None or m10 is None:
        return "中性"
    if m5 < -0.5:                                   # 点火后即还回去
        return "一日游"
    if m5 > 1.0 and m10 >= m5:                       # 越走越强
        return "持续型"
    if (m1 or 0) < 0 and m10 > 1.0:                  # 先歇后涨
        return "慢热型"
    return "中性"


# ── 单主题行 ─────────────────────────────────────────────────────────────────
def _theme_row(name: str, kind: str, k: pd.DataFrame,
               bench_close: pd.Series) -> dict | None:
    """聚合某主题历史所有点火事件 → 各期中位超额 + T+5 转正率 + 标签。"""
    if k is None or len(k) < MIN_BARS or "close" not in k.columns:
        return None
    theme_close = pd.Series(pd.to_numeric(k["close"], errors="coerce").values,
                            index=k["trade_date"].astype(str))
    igs = _find_ignitions(theme_close, bench_close)
    if not igs:
        return None
    events = [_forward_excess(theme_close, bench_close, i) for i in igs]
    med: dict[int, float] = {}
    for h in HORIZONS:
        vals = [e[h] for e in events if h in e]
        if vals:
            med[h] = round(median(vals), 2)
    t5 = [e[5] for e in events if 5 in e]
    return {
        "theme": name, "kind": kind, "n": len(igs),
        "label": _classify(len(igs), med),
        "fwd": {f"t{h}": med.get(h) for h in HORIZONS},
        "hit5": round(sum(v > 0 for v in t5) / len(t5) * 100, 1) if t5 else None,
        "last_ignition": theme_close.index[igs[-1]][4:],       # MMDD
    }


# ── 主构建（盘后·日缓存）─────────────────────────────────────────────────────
def build_theme_lifecycle(end: str, provider: CompositeProvider | None = None,
                          force: bool = False) -> dict:
    """全行业+概念主题生命周期归因·JSON日缓存。复用 _index_daily(与大周期榜共享缓存)。"""
    cdir = get_settings().cache_dir / "theme_lifecycle"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{end}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    prov = provider or CompositeProvider()
    start = (pd.Timestamp(end) - pd.Timedelta(days=1000)).strftime("%Y%m%d")
    bench = _benchmark_close(prov, start, end)
    if bench.empty:
        return {"ok": False, "msg": "基准(上证)数据为空", "rows": []}

    rows: list[dict] = []
    for kind, code_map in (("industry", _sw_code_map(prov)),
                           ("concept", _concept_code_map(prov, end))):
        for name, code in code_map.items():
            try:
                r = _theme_row(name, kind, _index_daily(prov, kind, code, end), bench)
                if r:
                    rows.append(r)
            except Exception as e:
                logger.debug("[主题生命周期] %s(%s) 失败: %s", name, kind, e)

    out = _assemble(end, rows)
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out


def _assemble(end: str, rows: list[dict]) -> dict:
    """汇总：按持续性(T+10中位超额)排序·全体alpha衰减曲线·参数/免责元信息。"""
    graded = [r for r in rows if r["label"] != "样本不足"]
    graded.sort(key=lambda r: (r["fwd"].get("t10") is not None,
                               r["fwd"].get("t10") or -999), reverse=True)
    decay = {}
    for h in HORIZONS:
        vals = [r["fwd"][f"t{h}"] for r in graded if r["fwd"].get(f"t{h}") is not None]
        decay[f"t{h}"] = round(median(vals), 2) if vals else None
    return {
        "ok": True, "end": end, "rows": graded,
        "insufficient": sorted([r["theme"] for r in rows if r["label"] == "样本不足"]),
        "decay_all": decay,
        "meta": {
            "ign_abs": IGN_ABS, "ign_excess": IGN_EXCESS, "cooldown": COOLDOWN,
            "min_events": MIN_EVENTS, "horizons": list(HORIZONS),
            "n_graded": len(graded), "n_total": len(rows),
            "disclaimer": ("点火日=板块指数当日涨≥%.0f%%且超上证≥%.1f点·近%d日去重；"
                           "数字=从点火收盘起各期**累计超额收益(板块−上证)中位数**。"
                           "历史结构描述·非预测·主题成分随时间变·样本<%d标样本不足。"
                           % (IGN_ABS, IGN_EXCESS, COOLDOWN, MIN_EVENTS)),
        },
    }
