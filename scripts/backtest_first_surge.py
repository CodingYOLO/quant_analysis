"""「首次放量启动」回测（Phase 0·先验证再上线——数字不行就不做后续）。

假设（2026-08-03 与用户讨论定稿）：
  盘中首次异常放量拉升本身不是买点（P(牛股|拉升)≈低·追当日强势在924后是负期望——
  因子归因实证 momentum→reversal），但作为**入池事件**+条件漏斗+回踩买点可能有效：
    长横盘(低波动箱体) + 低位 + 此前安静(首次) + 当日放量拉升 → T+1 观察 → 回踩确认再进。

⚠️口径限制（诚实声明）：幕数据 L1 无历史存档，"盘中拉升"用**日线代理**
（当日涨幅+当日量比），上线后盘中版与此回测存在口径差，需前向验证期校准。

两种入场对照（直接检验"追第一波 vs 等回踩"的历史差距）：
  A 追入：T+1 开盘买（T+1 一字/高开>9.5% 视为买不进·剔除）
  B 回踩确认：T+1 缩量(vol<T日) 且 低点不破 T 日开盘价 → T+2 开盘买；否则放弃

评价：T+5/T+20/T+60 收益(相对入场价)·胜率·中位数·最大被套(持有期内最低价/入场-1)
 vs 全市场同日基准；分年份；分大盘状态(上证收盘 vs MA20)。
参数敏感性：量比阈值 × 箱体窗口 网格。

用法：.venv/bin/python scripts/backtest_first_surge.py [--start 20230101] [--end 20260430]
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.composite_provider import CompositeProvider   # noqa: E402
from app.macro.sync import trading_days                     # noqa: E402

# ── 信号参数（BASE 为主口径·GRID 做敏感性） ────────────────────────────────
BASE = dict(box_win=40, box_amp=0.25, low_pos=0.5, quiet_win=20, quiet_vr=1.8,
            vr_min=2.5, chg_min=5.0)
GRID = [dict(BASE), dict(BASE, vr_min=2.0), dict(BASE, vr_min=3.0),
        dict(BASE, box_win=30), dict(BASE, box_win=60), dict(BASE, low_pos=0.99)]
HORIZONS = (5, 20, 60)


def load_panel(start: str, end: str) -> dict[str, pd.DataFrame]:
    """逐日 parquet → (date×stock) 宽表面板。只用本地/缓存数据·缺日直接报出来。"""
    prov = CompositeProvider()
    days = trading_days(start, end)
    frames, missing = [], []
    for d in days:
        try:
            df = prov.get_daily(d)
            if df is None or df.empty:
                missing.append(d)
                continue
            frames.append(df[["ts_code", "trade_date", "open", "high", "low", "close",
                              "pre_close", "vol", "pct_chg"]])
        except Exception:
            missing.append(d)
    if missing:
        print(f"⚠️ 缺 {len(missing)} 个交易日(如 {missing[:3]})——结果按现有数据计·不脑补")
    allf = pd.concat(frames, ignore_index=True)
    panel = {}
    for col in ("open", "high", "low", "close", "pre_close", "vol", "pct_chg"):
        panel[col] = (allf.pivot_table(index="trade_date", columns="ts_code", values=col)
                      .sort_index())
    print(f"面板: {panel['close'].shape[0]} 交易日 × {panel['close'].shape[1]} 股票")
    return panel


def eligible_mask(panel: dict, prov: CompositeProvider) -> pd.DataFrame:
    """基础可交易过滤：非ST·上市>120交易日·非北交所（低流动性&涨跌幅30%口径不同）。"""
    close = panel["close"]
    sb = prov.get_stock_basic()
    st = set(sb[sb["name"].fillna("").str.upper().str.contains("ST")]["ts_code"])
    keep_cols = [c for c in close.columns if c not in st and not c.endswith(".BJ")]
    # ⚠️不能用 rolling().count()：它数的是非NaN·布尔框恒等于窗口长——须 sum(True)
    listed = close[keep_cols].notna().rolling(120).sum() >= 120   # 面板内可见≥120日
    return listed


def signal_mask(panel: dict, ok: pd.DataFrame, p: dict) -> pd.DataFrame:
    """入池信号（全向量化·所有滚动窗均右端=昨日·无前视）。"""
    close, high, low, vol, chg = (panel[k] for k in ("close", "high", "low", "vol", "pct_chg"))
    prev = lambda df: df.shift(1)                                  # noqa: E731  昨日视角
    # 箱体(不含当日)：过去 box_win 日振幅 ≤ box_amp
    box_hi = prev(high).rolling(p["box_win"]).max()
    box_lo = prev(low).rolling(p["box_win"]).min()
    box = (box_hi - box_lo) / box_lo <= p["box_amp"]
    # 低位：昨日收盘处于箱体下 low_pos 分位内
    pos = (prev(close) - box_lo) / (box_hi - box_lo).replace(0, np.nan)
    low_ok = pos <= p["low_pos"]
    # 此前安静(首次)：过去 quiet_win 日 vol/vol20 全部 < quiet_vr 且无≥9.5%大阳
    vol20 = prev(vol).rolling(20).mean()
    vr_hist = (prev(vol) / prev(vol).rolling(20).mean())
    quiet = ((vr_hist < p["quiet_vr"]).rolling(p["quiet_win"]).min() > 0) & \
            ((prev(chg) < 9.5).rolling(p["quiet_win"]).min() > 0)
    # 当日：放量拉升(涨幅≥chg_min·量比≥vr_min·非一字[high>low])
    today = (chg >= p["chg_min"]) & (vol / vol20 >= p["vr_min"]) & (high > low)
    return (box & low_ok & quiet & today & ok).fillna(False)


def evaluate(panel: dict, sig: pd.DataFrame, tag: str) -> dict:
    """两种入场 × 三个持有期 → 胜率/均值/中位/最大被套 + 同日全市场基准。"""
    o, c, l, v = panel["open"], panel["close"], panel["low"], panel["vol"]
    dates = list(c.index)
    idx = {d: i for i, d in enumerate(dates)}
    rows = []
    for d, col_mask in sig.iterrows():
        i = idx[d]
        if i + 3 >= len(dates):
            continue
        d1, d2 = dates[i + 1], dates[i + 2]
        for ts in col_mask.index[col_mask.values]:
            open1 = o.at[d1, ts]
            if pd.isna(open1):
                continue
            gap1 = open1 / c.at[d, ts] - 1
            entry_a = open1 if gap1 <= 0.095 else np.nan      # A: 一字/近涨停开盘买不进
            # B: T+1 缩量且不破 T 日开盘 → T+2 开盘
            pull_ok = (v.at[d1, ts] < v.at[d, ts]) and (l.at[d1, ts] > o.at[d, ts])
            entry_b = o.at[d2, ts] if (pull_ok and pd.notna(o.at[d2, ts])) else np.nan
            row = {"date": d, "ts": ts}
            for mode, entry, ei in (("A", entry_a, i + 1), ("B", entry_b, i + 2)):
                if pd.isna(entry) or entry <= 0:
                    continue
                for h in HORIZONS:
                    if ei + h < len(dates):
                        row[f"{mode}_r{h}"] = c.iloc[ei + h][ts] / entry - 1
                hz = min(ei + 20, len(dates) - 1)
                row[f"{mode}_mdd20"] = l.iloc[ei:hz + 1][ts].min() / entry - 1
            rows.append(row)
    if not rows:
        return {"tag": tag, "n": 0}
    df = pd.DataFrame(rows)
    # 同日全市场基准（等权·T+1开盘→T+h收盘·近似）
    mkt = {}
    for h in HORIZONS:
        fwd = (c.shift(-1 - h) / o.shift(-1)).stack() - 1
        mkt[h] = float(fwd.loc[fwd.index.get_level_values(0).isin(df["date"].unique())].mean())
    out = {"tag": tag, "n": len(df), "n_dates": df["date"].nunique(),
           "per_day": round(len(df) / max(df["date"].nunique(), 1), 2),
           "b_fill_rate": round(df.filter(like="B_r").notna().any(axis=1).mean(), 3),
           "mkt": {h: round(mkt[h] * 100, 2) for h in HORIZONS}}
    for mode in ("A", "B"):
        m = {}
        for h in HORIZONS:
            col = df.get(f"{mode}_r{h}")
            if col is None or col.notna().sum() == 0:
                continue
            cc = col.dropna()
            m[f"T+{h}"] = {"n": len(cc), "胜率%": round((cc > 0).mean() * 100, 1),
                           "均值%": round(cc.mean() * 100, 2), "中位%": round(cc.median() * 100, 2),
                           "超额%": round(cc.mean() * 100 - mkt[h] * 100, 2)}
        mdd = df.get(f"{mode}_mdd20")
        if mdd is not None and mdd.notna().sum():
            m["被套20日中位%"] = round(mdd.dropna().median() * 100, 2)
            m["被套20日P10%"] = round(mdd.dropna().quantile(0.1) * 100, 2)
        out[mode] = m
    out["_df"] = df
    return out


def yearly_split(res: dict, panel: dict) -> pd.DataFrame:
    df = res.get("_df")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["year"] = df["date"].str[:4]
    g = []
    for y, sub in df.groupby("year"):
        row = {"年份": y, "信号数": len(sub)}
        for mode in ("A", "B"):
            col = sub.get(f"{mode}_r20")
            if col is not None and col.notna().sum() >= 10:
                cc = col.dropna()
                row[f"{mode} T+20胜率%"] = round((cc > 0).mean() * 100, 1)
                row[f"{mode} T+20均值%"] = round(cc.mean() * 100, 2)
        g.append(row)
    return pd.DataFrame(g)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20230101")
    ap.add_argument("--end", default="20260430")
    args = ap.parse_args()
    prov = CompositeProvider()
    # 面板向前多载 80 日供滚动窗热身
    warm = trading_days("20220901", args.start)
    panel = load_panel(warm[0] if warm else args.start, "20260731")
    ok = eligible_mask(panel, prov)
    sig_window = (slice(args.start, args.end))

    results = []
    for i, p in enumerate(GRID):
        sig = signal_mask(panel, ok, p).loc[sig_window]
        tag = f"vr≥{p['vr_min']}·箱{p['box_win']}日·低位≤{p['low_pos']}"
        res = evaluate(panel, sig, tag)
        results.append((p, res))
        print(f"\n═══ [{i}] {tag} ═══  信号 {res['n']} 个·{res.get('n_dates',0)} 天·"
              f"日均 {res.get('per_day','-')}·B成交率 {res.get('b_fill_rate','-')}")
        for mode in ("A", "B"):
            if mode in res:
                print(f"  {mode}({'T+1追入' if mode=='A' else '回踩确认T+2'}):",
                      {k: v for k, v in res[mode].items() if k.startswith("T+") or "被套" in k})
        print(f"  同期全市场基准: {res.get('mkt')}")
        if i == 0:
            ys = yearly_split(res, panel)
            if not ys.empty:
                print("  — 分年份(主口径) —")
                print(ys.to_string(index=False))
    return results


if __name__ == "__main__":
    main()
