"""熟票筛选器（/tstock）：量化"适合反复做/做T"的候选票（2026-08-05 用户定档）。

背景：用户收敛到"熟票战法"（标的专一·反复做波段/T）——选票决定80%成败，
本模块把选票标准全部量化（拒绝"拉出K线看感觉"）：

  硬门槛(gate)   流动性(120日成交额中位≥8亿) · 波动(日均振幅≥2.5%) · 历史≥220根 · 非ST
  区间性(25分)   价格效率系数 ER=|位移|/路径长(120日)——低=震荡反复·高=单边(不适合反复做)
  振幅(20分)     日均振幅 2.5%→5.5% 线性给分（做T空间）
  均线尊重(25分) 统计近一年触碰 MA10/20/30 后5日反弹率——"它认哪条线"算出来·不靠手工记
  习惯清晰(15分) 低开高走率/高开低走率偏离50%的程度——节奏可预测性(全部由日线OHLC统计)
  波动稳定(15分) 月度振幅变异系数——股性稳定的票·档案才有复用价值(游资票股性会漂)

Top 25 附加：ROE(因子表) · 行业月线方向(板块诊断缓存) · 事件避雷(减持/解禁/大宗·
fundamentals 模块)。**排雷只标注不静默剔除**——雷点比选强重要·但判断权留给用户。

诚实边界：①分时级习惯(早盘冲高回落型等)需分钟历史·幕数据存档尚在积累·本版全部
日线口径不硬凑；②"适合做T"是结构描述·未回测·做T本身对多数人是负期望——
是否真有edge必须由使用者自己的交易账本统计验证。非买卖建议。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

_LOOKBACK = 250          # 面板长度(交易日)：均线尊重/稳定性用全窗
_WIN = 120               # 区间性/振幅/习惯的统计窗(≈半年)
_MIN_BARS = 220          # 历史不足不评(次新股股性未定型)
_GATE_AMT_YI = 8.0       # 成交额中位下限(亿)
_GATE_AMP = 2.5          # 日均振幅下限(%)
_TOP_N = 25
_MA_WINS = (10, 20, 30)  # 候选"认线"
_TOUCH_TOL = 1.005       # 触线判定：最低价 ≤ MA×1.005
_MIN_TOUCHES = 6         # 触碰样本不足不认线(小样本反弹率无意义)


# ──────────────────────────────────────────────
# 纯函数指标（列=股票的透视表·全向量化·可单测）
# ──────────────────────────────────────────────

def efficiency_ratio(close: pd.DataFrame, win: int = _WIN) -> pd.Series:
    """价格效率系数(Kaufman ER)：|净位移|/路径总长。≈1=单边·≈0=原地反复(适合反复做)。"""
    seg = close.tail(win)
    disp = (seg.iloc[-1] - seg.iloc[0]).abs()
    path = seg.diff().abs().sum()
    return (disp / path.replace(0, np.nan)).astype(float)


def ma_bounce_stats(close: pd.DataFrame, low: pd.DataFrame, ma_win: int,
                    horizon: int = 5, tol: float = _TOUCH_TOL) -> tuple[pd.Series, pd.Series]:
    """触碰 MA 后的反弹率：(反弹率, 触碰次数)。

    触碰=昨日收盘在线上·今日最低探至 MA×tol 以内（回踩到线）；
    反弹=触碰日收盘起 horizon 日后收盘更高。右端不足 horizon 的触碰不计。
    """
    ma = close.rolling(ma_win).mean()
    touch = (low <= ma * tol) & (close.shift(1) > ma.shift(1))
    fwd_up = close.shift(-horizon) > close
    valid = touch & fwd_up.notna()
    touches = valid.sum()
    rate = (valid & fwd_up).sum() / touches.replace(0, np.nan)
    return rate.astype(float), touches.astype(int)


def habit_freqs(open_: pd.DataFrame, close: pd.DataFrame, pre_close: pd.DataFrame,
                win: int = _WIN) -> dict[str, pd.Series]:
    """日线可算的股性习惯频率：低开高走率 / 高开低走率（条件频率·分母为对应开盘类型日数）。"""
    o, c, pc = open_.tail(win), close.tail(win), pre_close.tail(win)
    gap_dn = o < pc * 0.998
    gap_up = o > pc * 1.002
    dn_recover = (gap_dn & (c > o)).sum() / gap_dn.sum().replace(0, np.nan)
    up_fade = (gap_up & (c < o)).sum() / gap_up.sum().replace(0, np.nan)
    return {"dn_recover": dn_recover.astype(float), "up_fade": up_fade.astype(float),
            "n_gap_dn": gap_dn.sum().astype(int), "n_gap_up": gap_up.sum().astype(int)}


def yang_follow_rate(open_: pd.DataFrame, close: pd.DataFrame, vol: pd.DataFrame,
                     win: int = _WIN, vr_th: float = 1.8) -> pd.Series:
    """放量阳线次日惯性：放量(量>20日均量×vr)且收阳的次日上涨率。"""
    vma = vol.rolling(20).mean()
    sig = ((vol > vma * vr_th) & (close > open_)).tail(win)
    nxt_up = (close.shift(-1) > close).tail(win)
    valid = sig & nxt_up.notna()
    return ((valid & nxt_up).sum() / valid.sum().replace(0, np.nan)).astype(float)


def amp_stats(high: pd.DataFrame, low: pd.DataFrame, pre_close: pd.DataFrame,
              win: int = _WIN) -> tuple[pd.Series, pd.Series]:
    """(日均振幅%, 月度振幅变异系数)。变异系数低=股性稳定·档案可复用。"""
    amp = ((high - low) / pre_close * 100).tail(win)
    avg = amp.mean()
    # 按 ~21 根一段(月)取段均值·段间std/均值=稳定性
    seg_means = [amp.iloc[i:i + 21].mean() for i in range(0, len(amp) - 20, 21)]
    seg = pd.DataFrame(seg_means)
    cv = (seg.std() / seg.mean().replace(0, np.nan)).iloc[:] if len(seg) >= 3 else pd.Series(
        np.nan, index=avg.index)
    if isinstance(cv, pd.DataFrame):
        cv = cv.iloc[0]
    return avg.astype(float), cv.astype(float)


def _ramp(v, lo, hi):
    return np.clip((v - lo) / (hi - lo), 0.0, 1.0)


def t_fit_score(er, amp, best_bounce, clarity, amp_cv) -> float:
    """T适配分 0-100（NaN 项按 0 计·纯函数可测）：
    区间性25(ER低好) + 振幅20 + 均线尊重25 + 习惯清晰15 + 波动稳定15(CV低好)。"""
    s = 0.0
    if pd.notna(er):
        s += 25 * (1 - _ramp(er, 0.15, 0.50))
    if pd.notna(amp):
        s += 20 * _ramp(amp, _GATE_AMP, 5.5)
    if pd.notna(best_bounce):
        s += 25 * _ramp(best_bounce, 0.50, 0.75)
    if pd.notna(clarity):
        s += 15 * _ramp(clarity, 0.05, 0.30)
    if pd.notna(amp_cv):
        s += 15 * (1 - _ramp(amp_cv, 0.15, 0.50))
    return round(float(s), 1)


# ──────────────────────────────────────────────
# 面板加载与主流程
# ──────────────────────────────────────────────

def _load_panel(end: str, provider: CompositeProvider) -> dict[str, pd.DataFrame]:
    """近 _LOOKBACK 交易日 × 全市场 OHLC 面板（逐日 parquet 缓存·缺日跳过并报数量）。"""
    from app.macro.sync import trading_days
    start = (pd.Timestamp(end) - pd.Timedelta(days=int(_LOOKBACK * 1.7))).strftime("%Y%m%d")
    days = trading_days(start, end)[-_LOOKBACK:]
    frames, missing = [], 0
    for d in days:
        try:
            df = provider.get_daily(d)
            if df is None or df.empty:
                missing += 1
                continue
            frames.append(df[["ts_code", "trade_date", "open", "high", "low", "close",
                              "pre_close", "vol", "amount"]])
        except Exception:
            missing += 1
    if missing:
        logger.warning("[熟票筛选] 缺 %d/%d 个交易日·按现有数据计不脑补", missing, len(days))
    allf = pd.concat(frames, ignore_index=True)
    return {c: allf.pivot_table(index="trade_date", columns="ts_code", values=c).sort_index()
            for c in ("open", "high", "low", "close", "pre_close", "vol", "amount")}


def build_t_candidates(end: str, provider: CompositeProvider | None = None,
                       force: bool = False) -> dict:
    """熟票候选榜（周缓存——股性变化以月计·不必日更）。"""
    import json

    from app.config import get_settings
    prov = provider or CompositeProvider()
    cdir = get_settings().cache_dir / "t_screener"
    cdir.mkdir(parents=True, exist_ok=True)
    iso = pd.Timestamp(end).isocalendar()
    cache = cdir / f"{iso.year}W{iso.week:02d}_v1.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    p = _load_panel(end, prov)
    close, low, high = p["close"], p["low"], p["high"]
    n_bars = close.notna().sum()

    # ── 硬门槛 ──
    sb = prov.get_stock_basic()
    st_codes = set(sb[sb["name"].fillna("").str.upper().str.contains("ST")]["ts_code"])
    amt_med = (p["amount"].tail(_WIN) / 1e5).median()            # 千元→亿
    amp_avg, amp_cv = amp_stats(high, low, p["pre_close"])
    ok = ((n_bars >= _MIN_BARS) & (amt_med >= _GATE_AMT_YI) & (amp_avg >= _GATE_AMP)
          & ~close.columns.isin(st_codes) & ~close.columns.str.endswith(".BJ"))
    cols = close.columns[ok]
    logger.info("[熟票筛选] 门槛通过 %d/%d", len(cols), close.shape[1])

    # ── 指标(仅对过门槛列算·省时) ──
    sub = {k: v[cols] for k, v in p.items()}
    er = efficiency_ratio(sub["close"])
    habits = habit_freqs(sub["open"], sub["close"], sub["pre_close"])
    yang = yang_follow_rate(sub["open"], sub["close"], sub["vol"])
    bounce = {}
    for w in _MA_WINS:
        r, n = ma_bounce_stats(sub["close"], sub["low"], w)
        bounce[w] = (r.where(n >= _MIN_TOUCHES), n)

    name_map = dict(zip(sb["ts_code"], sb["name"]))
    ind_map = dict(zip(sb["ts_code"], sb.get("industry", pd.Series(dtype=str))))
    from app.strategy.ambush_board import _industry_monthly_map, board_of
    ind_dir = _industry_monthly_map(end)
    roe_map = _roe_map(end)

    rows = []
    for ts in cols:
        rates = {w: bounce[w][0].get(ts) for w in _MA_WINS}
        valid = {w: r for w, r in rates.items() if pd.notna(r)}
        best_ma, best_rate = (max(valid, key=valid.get), valid[max(valid, key=valid.get)]) \
            if valid else (None, np.nan)
        dnr, upf = habits["dn_recover"].get(ts), habits["up_fade"].get(ts)
        clarity = np.nanmax([abs((dnr or 0.5) - 0.5), abs((upf or 0.5) - 0.5)])
        score = t_fit_score(er.get(ts), amp_avg.get(ts), best_rate, clarity, amp_cv.get(ts))
        ind = str(ind_map.get(ts) or "")
        rows.append({
            "ts_code": ts, "name": name_map.get(ts, ts), "board": board_of(ts),
            "industry": ind, "ind_monthly": (ind_dir.get(ind) or {}).get("monthly_dir"),
            "score": score,
            "amt_med": round(float(amt_med.get(ts)), 1),
            "amp": round(float(amp_avg.get(ts)), 2),
            "amp_cv": round(float(amp_cv.get(ts)), 2) if pd.notna(amp_cv.get(ts)) else None,
            "er": round(float(er.get(ts)), 3) if pd.notna(er.get(ts)) else None,
            "best_ma": best_ma,
            "best_rate": round(float(best_rate) * 100, 1) if pd.notna(best_rate) else None,
            "touches": int(bounce[best_ma][1].get(ts)) if best_ma else 0,
            "dn_recover": round(float(dnr) * 100, 1) if pd.notna(dnr) else None,
            "up_fade": round(float(upf) * 100, 1) if pd.notna(upf) else None,
            "yang_follow": round(float(yang.get(ts)) * 100, 1) if pd.notna(yang.get(ts)) else None,
            "roe": roe_map.get(ts),
        })
    rows.sort(key=lambda x: -x["score"])
    top = rows[:_TOP_N]
    _attach_events(top)                                          # 只对Top做事件避雷(逐股API)

    out = {
        "ok": True, "end": end, "n_pass": len(rows), "n_universe": int(close.shape[1]),
        "rows": top,
        "note": ("门槛：120日成交额中位≥8亿+日均振幅≥2.5%+历史≥220根+非ST非北交。"
                 "T适配分=区间性25(ER低=震荡反复)+振幅20+均线尊重25(触线≥6次才认)+习惯清晰15"
                 "(低开高走/高开低走偏离50%)+波动稳定15(月度振幅CV低=股性不漂)。"
                 "全部日线口径·分时级习惯(早盘节奏等)待幕数据存档积累后加入。"
                 "周更缓存(股性以月计)。⚠️描述档·未回测·'适合做T'≠做T有正期望——"
                 "是否有edge必须由你自己的交易账本统计验证。雷点只标注不代删·非买卖建议。"),
    }
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out


def _roe_map(end: str) -> dict:
    """最新一份因子表的 ROE（基本面底线参考）。缺表→空。"""
    try:
        from app.config import get_settings
        fs = sorted((get_settings().cache_dir / "factor_table").glob("*.parquet"))
        if not fs:
            return {}
        df = pd.read_parquet(fs[-1], columns=["roe"])
        return {str(k): (round(float(v), 1) if pd.notna(v) else None)
                for k, v in df["roe"].items()}
    except Exception:
        return {}


def _attach_events(rows: list[dict]) -> None:
    """Top 候选逐股挂事件避雷摘要（减持/解禁/大宗折价·fundamentals 模块·失败留空不编）。"""
    from app.strategy.fundamentals import get_financials
    for r in rows:
        flags = []
        try:
            f = get_financials(r["ts_code"])
            ev = (f or {}).get("events") or {}
            ht = ev.get("holder_trade") or {}
            if (ht.get("de_count") or 0) > 0:
                flags.append(f"减持{ht['de_count']}次")
            fl = ev.get("float") or {}
            if fl.get("next_days") is not None and fl["next_days"] <= 60:
                flags.append(f"解禁{fl['next_days']}天后")
            bl = ev.get("block") or {}
            if bl.get("premium_avg") is not None and bl["premium_avg"] <= -3:
                flags.append(f"大宗折价{bl['premium_avg']}%")
            if (f or {}).get("summary"):
                r["fin_brief"] = f["summary"][:60]
        except Exception:
            pass
        r["risk_flags"] = flags
