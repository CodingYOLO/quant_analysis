"""
因子盈利归因（诚实版·标准量化因子检验）——回答"我们哪些选股因子真有前瞻alpha、哪些是噪音"。

对标吴川"找真正赚钱的因子"·但用**标准横截面因子检验**替代他的裸胜率：
  - **Rank-IC**：每个截面日·因子值与未来收益的 Spearman 秩相关(横截面·天然市场中性)。
  - **IC_IR**：mean(IC)/std(IC)·衡量**稳定性**(单看均值会被个别截面带偏)。
  - **分层多空价差**：按因子分5层·最高层−最低层的未来收益(long-short·对冲掉大盘)。

为何天然诚实：IC 是横截面排序相关、分层价差是多空对冲——**都不含市场beta**·不会像
裸胜率那样"牛市里啥都涨"。复用 `build_factor_table`(与选股同一套因子定义·非另造)。

护栏：样本门槛(n_periods<MIN 标样本不足) / 报 IC+IR+n 三件套非单一数 / 非重叠截面
(step=horizon·样本独立) / 免责"1年≈一个regime·因子有效性随市场变"。
"""

from __future__ import annotations

import json
import logging
from statistics import mean, pstdev

import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

# ── 参数 ─────────────────────────────────────────────────────────────────────
STEP = 10                 # 截面间隔(交易日)·= max horizon → 非重叠·样本独立
HORIZONS = (5, 10)        # 前瞻期(交易日)
N_PERIODS_MAX = 26        # 最多截面数(≈1年)
PANEL_LOOKBACK = 290      # 复权收盘面板回看(交易日)·= 26期×10 + 前瞻缓冲
QUANTILES = 5             # 分层数
MIN_CIRC_MV = 30.0        # 流通市值下限(亿)·剔除微盘(因子信号在微盘上多为噪音)
MIN_STOCKS = 200          # 单截面过滤后最少股票数
MIN_PERIODS = 10          # 样本门槛：有效截面<此 → 样本不足

# 待检因子(col, label)·取覆盖率高的alpha候选·排除 forecast_chg(3%覆盖)/纯描述估值
STUDY_FACTORS = [
    ("rps120", "RPS120(120日相对强度)"), ("rps50", "RPS50"),
    ("lead_resist", "🐲领涨抗跌分(近20日)"), ("lead_resist_mid", "领涨抗跌分(55日)"),
    ("up_excess", "涨时跑赢%"), ("down_excess", "跌时跑赢%(抗跌)"),
    ("rel5d", "近5日跑赢大盘%"), ("rel3d", "近3日跑赢大盘%"),
    ("ret20", "近20日涨幅%"), ("ret5", "近5日涨幅%"),
    ("ma20_slope", "MA20斜率%"), ("obv_slope", "OBV斜率(吸筹)"),
    ("accum_score", "🐌吸筹评分"), ("squeeze_pctile", "蓄势收窄分位"),
    ("main_net_amount", "主力净流入(亿)"), ("main_net_3d", "主力近3日(亿)"),
    ("inflow_days_10", "主力流入天数(近10)"), ("consec_inflow", "连续净流入天数"),
    ("inst_net_yi", "龙虎榜机构真钱(亿)"), ("youzi_relay_days", "游资接力天数"),
    ("vol5_vol20", "量能比5/20"), ("turnover_rate", "换手率%"), ("volume_ratio", "量比"),
    ("comment_score", "千评得分"), ("roe", "ROE%"),
    ("circ_mv_100m", "流通市值(亿)"), ("pct_chg", "当日涨跌%"),
]


# ── 前瞻收益（复权收盘面板·横截面）───────────────────────────────────────────
def _fwd_return(panel: pd.DataFrame, dcol: int, h: int) -> pd.Series:
    """从面板第 dcol 列(截面日)到 dcol+h 列的收益%·index=ts_code。越界→空。"""
    if dcol + h >= panel.shape[1]:
        return pd.Series(dtype=float)
    p0 = pd.to_numeric(panel.iloc[:, dcol], errors="coerce")
    p1 = pd.to_numeric(panel.iloc[:, dcol + h], errors="coerce")
    return ((p1 / p0 - 1) * 100).replace([float("inf"), float("-inf")], pd.NA)


# ── 单因子·单截面：Rank-IC + 分层多空价差 ────────────────────────────────────
def _ic_and_spread(fval: pd.Series, fwd: pd.Series) -> tuple[float, float] | None:
    """对齐因子值与前瞻收益→(Spearman IC, 最高分层−最低分层收益)。样本不足→None。"""
    df = pd.concat([fval.rename("f"), fwd.rename("r")], axis=1).dropna()
    if len(df) < MIN_STOCKS or df["f"].nunique() < QUANTILES:
        return None
    ic = df["f"].rank().corr(df["r"].rank())       # Spearman=秩的Pearson·免 scipy 依赖
    try:
        q = pd.qcut(df["f"].rank(method="first"), QUANTILES, labels=False)
    except ValueError:
        return None
    grp = df["r"].groupby(q).mean()
    if 0 not in grp.index or (QUANTILES - 1) not in grp.index:
        return None
    spread = grp[QUANTILES - 1] - grp[0]           # 高因子层 − 低因子层(long-short)
    return (round(float(ic), 4), round(float(spread), 3)) if pd.notna(ic) else None


# ── 聚合某因子跨所有截面 ─────────────────────────────────────────────────────
def _agg_factor(ics: list[float], spreads: list[float]) -> dict:
    """IC均值/IC_IR/同号率/多空价差均值/样本数 + 分级判定。"""
    n = len(ics)
    if n < MIN_PERIODS:
        return {"n": n, "verdict": "样本不足"}
    mic = mean(ics)
    sd = pstdev(ics) or 1e-9
    ir = mic / sd
    same = sum(1 for x in ics if (x > 0) == (mic > 0)) / n * 100
    return {
        "n": n, "mean_ic": round(mic, 4), "ic_ir": round(ir, 2),
        "ic_win": round(same, 1), "spread": round(mean(spreads), 2),
        "verdict": _verdict(mic, ir),
    }


def _verdict(mic: float, ir: float) -> str:
    """标准阈值：|IC|<0.02 噪音；≥0.02 有效(≥0.05且|IR|≥0.5 为强)；方向由IC符号。"""
    if abs(mic) < 0.02:
        return "无效·噪音"
    strength = "强" if (abs(mic) >= 0.05 and abs(ir) >= 0.5) else "有效"
    return f"{'正向' if mic > 0 else '反向'}·{strength}"


# ── 主构建（盘后·日缓存·回填历史因子表·较重）────────────────────────────────
def build_factor_efficacy(end: str, provider: CompositeProvider | None = None,
                          force: bool = False) -> dict:
    """全因子·多期IC/分层归因·JSON日缓存。回填 STEP 间隔的历史因子表(顺带填缓存)。"""
    cdir = get_settings().cache_dir / "factor_efficacy"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{end}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    from app.factors.breadth_qfq import build_qfq_panel
    from app.strategy.screener import build_factor_table
    prov = provider or CompositeProvider()
    panel = build_qfq_panel(end, prov, lookback=PANEL_LOOKBACK)
    if panel is None or panel.empty or panel.shape[1] < STEP * 3:
        return {"ok": False, "msg": "复权面板不足", "rows": []}

    cols = list(panel.columns)
    reb_idx = _rebalance_cols(len(cols), STEP, max(HORIZONS), N_PERIODS_MAX)
    # {factor: {h: {"ic": [...], "spread": [...]}}}
    acc: dict = {c: {h: {"ic": [], "spread": []} for h in HORIZONS} for c, _ in STUDY_FACTORS}
    used = 0
    for di in reb_idx:
        d = cols[di]
        try:
            ft = build_factor_table(d, prov)
        except Exception as e:
            logger.warning("[因子归因] %s 因子表失败·跳过: %s", d, e)
            continue
        _accumulate(ft, panel, di, acc)
        used += 1

    rows = _build_rows(acc)
    out = _assemble(end, rows, used, cols[reb_idx[0]] if reb_idx else "", end)
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out


def load_latest_efficacy() -> dict | None:
    """读最新一份因子归因缓存(端点用·不阻塞·不触发重建)。无→None。"""
    cdir = get_settings().cache_dir / "factor_efficacy"
    files = sorted(cdir.glob("*.json")) if cdir.exists() else []
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def _rebalance_cols(n_cols: int, step: int, max_h: int, n_max: int) -> list[int]:
    """面板列位置里的截面点：留出 max_h 前瞻缓冲·从末端往前按 step 取·至多 n_max 个。"""
    last = n_cols - max_h - 1
    idx = list(range(last, -1, -step))[:n_max]
    return sorted(idx)


def _accumulate(ft: pd.DataFrame, panel: pd.DataFrame, di: int, acc: dict) -> None:
    """单截面：过滤流通市值→逐因子逐期算IC/价差→累加。"""
    if "ts_code" not in ft.columns:
        return
    f = ft.set_index("ts_code")
    if "circ_mv_100m" in f.columns:
        f = f[pd.to_numeric(f["circ_mv_100m"], errors="coerce") >= MIN_CIRC_MV]
    fwds = {h: _fwd_return(panel, di, h) for h in HORIZONS}
    for col, _ in STUDY_FACTORS:
        if col not in f.columns:
            continue
        fval = pd.to_numeric(f[col], errors="coerce")
        for h in HORIZONS:
            res = _ic_and_spread(fval, fwds[h])
            if res:
                acc[col][h]["ic"].append(res[0])
                acc[col][h]["spread"].append(res[1])


def _build_rows(acc: dict) -> list[dict]:
    """每因子 → {factor,label, h5:{...}, h10:{...}}。"""
    label = dict(STUDY_FACTORS)
    rows = []
    for col, _ in STUDY_FACTORS:
        row = {"factor": col, "label": label[col]}
        for h in HORIZONS:
            row[f"h{h}"] = _agg_factor(acc[col][h]["ic"], acc[col][h]["spread"])
        rows.append(row)
    return rows


def _assemble(end: str, rows: list[dict], n_used: int, start: str, stop: str) -> dict:
    """按 T+10 |IC| 排序(样本不足沉底)·附参数/免责。"""
    def keyf(r):
        h = r.get("h10", {})
        return abs(h.get("mean_ic", 0)) if h.get("verdict") != "样本不足" else -1
    rows.sort(key=keyf, reverse=True)
    return {
        "ok": True, "end": end, "rows": rows,
        "meta": {
            "n_periods": n_used, "step": STEP, "horizons": list(HORIZONS),
            "quantiles": QUANTILES, "min_circ_mv": MIN_CIRC_MV, "min_periods": MIN_PERIODS,
            "date_range": f"{start}→{stop}",
            "disclaimer": ("IC=横截面因子值与未来收益的Spearman秩相关(市场中性)·"
                           "IC_IR=IC均值/波动(稳定性)·分层价差=最高层−最低层收益(多空对冲)。"
                           "|IC|<0.02≈噪音·≥0.02有效(≥0.05且|IR|≥0.5为强)·方向看IC符号。"
                           "样本≈近1年(%d期·非重叠)·因子有效性随市场regime变·历史检验非预测。"
                           % n_used),
        },
    }
