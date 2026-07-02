"""
板块诊断·特征引擎（真时点成分 + 双分母资金 + 向量化多周期宽度）。全部 point-in-time·无未来函数。

对 [start,end] 每交易日 × 各申万板块产出：
  原始： net(主力净流入亿·Tushare官方估算) / circ(流通市值亿) / amt(成交额亿) / pct(成分中位涨幅%)
         宽度 ma5/ma10/ma20/ma60（成分中站上前复权均线占比%·向量化一次算全轨迹）
  派生： pen=net/circ(慢稳分母·渗透率) · press=net/amt(快敏分母·买入压力占比)
         pen_z/press_z（横截面稳健标准化·**仅供"今天谁最强"排序**）
         pen_accel=pen自身差分（**加速度只用稳定分母**·不用press占比差分·避免分母抖动污染衰竭）
         pen_f3d/f5d, press_f3d/f5d（近3/5日滚动和·自身趋势）

成分：真时点（sw_membership.members_asof·每日按 in_date/out_date 重建）——消除成分漂移幸存者偏差。
⚠️ 残余：Tushare 成分库不含已退市股（有界·往后收敛的残差）。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.factors.breadth_qfq import _recent_trade_dates, build_qfq_panel
from app.strategy.sw_membership import load_history, members_asof

logger = logging.getLogger(__name__)

_MA = (5, 10, 20, 60)
_ROLL_PRE = 6                                      # F3d/F5d 需信号段前多取几天


def build_features(end: str, start: str, provider: CompositeProvider | None = None,
                   level: str = "L2", hist: int = 150) -> dict:
    """构建 [start,end] 各板块日度特征（真时点成分·双分母·向量化宽度）。

    end/start: YYYYMMDD。level: L1/L2/L3。hist: 宽度均线回看余量(覆盖MA60+)。
    Returns: {dates:[...out...], level, sectors:{name:{net,circ,amt,pct,ma*,pen,press,pen_z,press_z,
             pen_accel,pen_f3d,pen_f5d,press_f3d,press_f5d,n}}}。
    """
    provider = provider or CompositeProvider()
    memhist = load_history(provider)
    if memhist.empty:
        raise ValueError("申万成分历史为空")

    # 1) 交易日：输出段 [start,end] + 前 _ROLL_PRE 天(滚动) ；宽度靠 panel 的更长回看
    all_dates = _recent_trade_dates(provider, end, hist + 400)      # 足够长(升序)
    all_dates = [d for d in all_dates if d <= end]
    out_dates = [d for d in all_dates if start <= d <= end]
    if not out_dates:
        raise ValueError(f"[{start},{end}] 无交易日")
    pre_i = max(0, all_dates.index(out_dates[0]) - _ROLL_PRE)
    calc_dates = all_dates[pre_i:all_dates.index(out_dates[-1]) + 1]   # 计算段(含前置滚动)

    # 2) 向量化多周期宽度：全市场 close×adj 面板 → 各档 (close≥MA) 布尔面板(一次算)
    span = len(calc_dates)
    panel = build_qfq_panel(end, provider, lookback=span + hist)   # 后复权(close×adj_d)·见 _breadth_panels
    pre_cols = [c for c in panel.columns if c < out_dates[0]]       # 暖机校验：信号起前的面板天数
    if len(pre_cols) < max(_MA):
        logger.warning("[诊断] 宽度暖机不足：信号起 %s 前仅 %d 交易日(<MA%d)·长周期宽度可能残缺",
                       out_dates[0], len(pre_cols), max(_MA))
    above, valid = _breadth_panels(panel, _MA)

    # 3) 逐日 per-stock 特征(net/circ/amt/pct)——只在计算段
    day_feat: dict[str, pd.DataFrame] = {}
    for d in calc_dates:
        day_feat[d] = _stock_features(provider, d)

    # 4) 逐日 × 各板块 真时点聚合
    date_cols = set(panel.columns)                                 # 面板"日期"集合(判宽度可算)
    panel_stocks = set(panel.index)                                # 面板"股票"集合(判成分在册)
    raw = {}                                                       # name -> {net:[],...}(计算段长度)
    for d in calc_dates:
        mmap = members_asof(memhist, d, level)                     # 真时点成分
        feat = day_feat[d]
        has_breadth = d in date_cols
        for name, codes in mmap.items():
            fcodes = [c for c in codes if c in feat.index]         # 有资金/市值数据的成分
            bcodes = [c for c in codes if c in panel_stocks]       # 有价格(宽度)的成分
            rec = raw.setdefault(name, {k: [] for k in
                  ("net", "circ", "amt", "pct", "n", "ma5", "ma10", "ma20", "ma60")})
            if not fcodes and not bcodes:
                for k in rec:
                    rec[k].append(None)
                continue
            sub = feat.loc[fcodes] if fcodes else feat.iloc[:0]
            rec["net"].append(_nsum(sub["net"]))
            rec["circ"].append(_nsum(sub["circ"]))
            rec["amt"].append(_nsum(sub["amt"]))
            rec["pct"].append(_nmed(sub["pct"]))
            rec["n"].append(int(sub["net"].notna().sum()) if fcodes else 0)
            for w in _MA:
                if has_breadth and bcodes:
                    den = float(valid[w].loc[bcodes, d].sum())     # 有有效均线的成分数
                    num = float(above[w].loc[bcodes, d].sum())     # 其中站上的
                    rec[f"ma{w}"].append(round(num / den * 100, 1) if den else None)
                else:
                    rec[f"ma{w}"].append(None)

    # 5) 派生特征（双分母 + 横截面标准化 + 加速度/滚动）→ 只输出 out_dates 段
    trim = len(calc_dates) - len(out_dates)
    sectors = _derive(raw, calc_dates, trim)
    return {"dates": out_dates, "level": level, "sectors": sectors}


def _breadth_panels(panel: pd.DataFrame, windows=_MA) -> tuple[dict, dict]:
    """全市场 (close≥MAw) 布尔面板 + 有效均线掩码。

    **复权口径一致性**：panel = close×adj_factor(当日) = **后复权**·MA 与 close 同口径同一序列。
    后复权 adj_factor 为历史固定值（不随未来除权变动），故不引入"历史MA随未来除权平移"的前视
    （前复权才有此问题·此处刻意用后复权）。对「≥」判断，后复权与前复权等价（线性缩放不改大小）。
    **暖机**：min_periods=w → 不足 w 日的（次新股/面板头部）MA=NaN → valid=False 剔除·不算残缺值。
    """
    above, valid = {}, {}
    for w in windows:
        ma = panel.T.rolling(w, min_periods=w).mean().T            # 沿日期轴滚动均线
        valid[w] = ma.notna() & panel.notna()                      # 均线有效(次新股/暖机不足→剔·不稀释)
        above[w] = panel >= ma                                     # True 仅当有效且站上
    return above, valid


def _stock_features(provider, d: str) -> pd.DataFrame:
    """某日 per-stock: net(净流入亿)/circ(流通市值亿)/amt(成交额亿)/pct(涨跌%)·index=ts_code。"""
    idx = pd.Index([], name="ts_code")
    net = circ = amt = pct = pd.Series(dtype=float)
    mf = provider.get_money_flow(d)
    if mf is not None and not mf.empty and "net_mf_amount" in mf.columns:
        net = pd.to_numeric(mf.set_index("ts_code")["net_mf_amount"], errors="coerce") / 1e4  # 万→亿
    db = provider.get_daily_basic(d)
    if db is not None and not db.empty and "circ_mv" in db.columns:
        circ = pd.to_numeric(db.set_index("ts_code")["circ_mv"], errors="coerce") / 1e4        # 万→亿
    dl = provider.get_daily(d)
    if dl is not None and not dl.empty:
        dl = dl.set_index("ts_code")
        if "amount" in dl.columns:
            amt = pd.to_numeric(dl["amount"], errors="coerce") / 1e5                            # 千→亿
        if "pct_chg" in dl.columns:
            pct = pd.to_numeric(dl["pct_chg"], errors="coerce")
    out = pd.DataFrame({"net": net, "circ": circ, "amt": amt, "pct": pct})
    return out


def _nsum(s: pd.Series):
    v = pd.to_numeric(s, errors="coerce").dropna()
    return round(float(v.sum()), 3) if len(v) else None


def _nmed(s: pd.Series):
    v = pd.to_numeric(s, errors="coerce").dropna()
    return round(float(v.median()), 3) if len(v) else None


def _derive(raw: dict, calc_dates: list, trim: int) -> dict:
    """由原始序列派生 pen/press/横截面z/加速度/滚动，并裁到输出段。"""
    names = list(raw.keys())
    n_days = len(calc_dates)
    # 双分母(逐日逐板块)
    pen = {nm: [_safe_div(raw[nm]["net"][i], raw[nm]["circ"][i]) for i in range(n_days)] for nm in names}
    press = {nm: [_safe_div(raw[nm]["net"][i], raw[nm]["amt"][i]) for i in range(n_days)] for nm in names}
    # 横截面稳健标准化(每日独立·仅排序用)
    pen_z = _cross_z(pen, names, n_days)
    press_z = _cross_z(press, names, n_days)

    out = {}
    for nm in names:
        p, pr = pen[nm], press[nm]
        rec = raw[nm]
        s = {
            "net": rec["net"][trim:], "circ": rec["circ"][trim:], "amt": rec["amt"][trim:],
            "pct": rec["pct"][trim:], "n": rec["n"][trim:],
            "ma5": rec["ma5"][trim:], "ma10": rec["ma10"][trim:],
            "ma20": rec["ma20"][trim:], "ma60": rec["ma60"][trim:],
            "pen": p[trim:], "press": pr[trim:],
            "pen_z": pen_z[nm][trim:], "press_z": press_z[nm][trim:],
            "pen_accel": [_diff(p, i) for i in range(trim, n_days)],       # 加速度=pen自身差分(稳定分母)
            "pen_f3d": [_rollsum(p, i, 3) for i in range(trim, n_days)],
            "pen_f5d": [_rollsum(p, i, 5) for i in range(trim, n_days)],
            "press_f3d": [_rollsum(pr, i, 3) for i in range(trim, n_days)],
            "press_f5d": [_rollsum(pr, i, 5) for i in range(trim, n_days)],
        }
        out[nm] = s
    return out


def _safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return round(a / b * 100, 4)                                   # ×100 便于阅读(渗透率%/压力%)


def _cross_z(series_map: dict, names: list, n_days: int) -> dict:
    """每日横截面稳健 z-score(中位/MAD)·返回 {name:[z...]}。仅供排序。"""
    z = {nm: [None] * n_days for nm in names}
    for i in range(n_days):
        vals = [(nm, series_map[nm][i]) for nm in names if series_map[nm][i] is not None]
        if len(vals) < 5:
            continue
        arr = np.array([v for _, v in vals], dtype=float)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med))) * 1.4826 or (float(arr.std()) or 1.0)
        for nm, v in vals:
            z[nm][i] = round((v - med) / mad, 2)
    return z


def _diff(seq: list, i: int):
    if i == 0 or seq[i] is None or seq[i - 1] is None:
        return None
    return round(seq[i] - seq[i - 1], 4)


def _rollsum(seq: list, i: int, n: int):
    lo = max(0, i - n + 1)
    vals = [v for v in seq[lo:i + 1] if v is not None]
    return round(sum(vals), 4) if vals else None
