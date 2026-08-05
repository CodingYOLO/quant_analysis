"""「双冰点」回测（Phase 0·用户假设：情绪冰点+成交额冰点后常有暴力反弹）。

预注册口径（先定死再看结果·不钓鱼）：
  量冰点   = 全A成交额5日均 处于过去250交易日 ≤10分位（point-in-time·右端=当日）
  情绪冰点 = 涨停触及数(日涨幅≥9.7%家数)5日均 ≤10分位；变体B用 5日线上占比≤25%
  双冰点   = 量冰点 AND 情绪冰点（同日成立）
  事件     = 连续信号日合并(间隔>10交易日=新事件)·入场基准=事件首日收盘(点-in-time可执行)
  评价     = 等权全A 与 上证 的 T+5/10/20/60 收益·逐事件列表(小样本诚实·不只给均值)
  敏感性   = 分位阈 10/15/20 三档

用法：.venv/bin/python scripts/backtest_double_ice.py [--start 20230601] [--end 20260805]
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


def load_series(start: str, end: str) -> pd.DataFrame:
    """逐日面板 → 日频序列表：amt(亿)/limit_touch(家)/breadth5(%)/eqw_ret(%)/sh_close。"""
    prov = CompositeProvider()
    days = trading_days(start, end)
    rows, closes = [], {}
    for d in days:
        try:
            df = prov.get_daily(d)
        except Exception:
            df = None
        if df is None or df.empty:
            continue
        px = pd.to_numeric(df["pct_chg"], errors="coerce")
        rows.append({
            "date": d,
            "amt": float(pd.to_numeric(df["amount"], errors="coerce").sum() / 1e5),
            "limit_touch": int((px >= 9.7).sum()),
            "eqw_ret": float(px.mean()),
        })
        closes[d] = df.set_index("ts_code")["close"]
    ser = pd.DataFrame(rows).set_index("date")

    # 5日线上占比：用收盘价矩阵滚动算（与情绪页口径一致方向·此处自算保证长窗）
    cl = pd.DataFrame(closes).T.sort_index()
    ma5 = cl.rolling(5).mean()
    ser["breadth5"] = ((cl > ma5).sum(axis=1) / cl.notna().sum(axis=1) * 100).reindex(ser.index)

    sh = prov.get_index_daily_range("000001.SH", start, end).sort_values("trade_date")
    ser["sh_close"] = sh.set_index("trade_date")["close"].reindex(ser.index)
    ser["eqw_cum"] = (1 + ser["eqw_ret"] / 100).cumprod()
    return ser


def pct_rank_right(s: pd.Series, win: int = 250, min_n: int = 200) -> pd.Series:
    """point-in-time 滚动分位(0-100)·右端=自身。"""
    return s.rolling(win, min_periods=min_n).rank(pct=True) * 100


def episodes_from(sig: pd.Series, gap: int = 10) -> list[str]:
    """信号日 → 事件首日列表（间隔>gap交易日=新事件）。"""
    idx = np.where(sig.values)[0]
    out = []
    for i in idx:
        if not out or i - out[-1][1] > gap:
            out.append([i, i])
        else:
            out[-1][1] = i
    return [sig.index[a] for a, _ in out]


def fwd_ret(ser: pd.DataFrame, d0: str, col: str, h: int) -> float | None:
    i = ser.index.get_loc(d0)
    if i + h >= len(ser):
        return None
    if col == "eqw":
        return round((ser["eqw_cum"].iloc[i + h] / ser["eqw_cum"].iloc[i] - 1) * 100, 2)
    return round((ser["sh_close"].iloc[i + h] / ser["sh_close"].iloc[i] - 1) * 100, 2)


def run(start: str, end: str) -> None:
    warm = trading_days("20220901", start)
    ser = load_series(warm[0] if warm else start, end)
    print(f"序列: {len(ser)} 交易日  {ser.index[0]}~{ser.index[-1]}")

    amt5 = ser["amt"].rolling(5).mean()
    lim5 = ser["limit_touch"].rolling(5).mean()
    p_amt = pct_rank_right(amt5)
    p_lim = pct_rank_right(lim5)

    for th in (10, 15, 20):
        sig = (p_amt <= th) & (p_lim <= th) & (ser.index >= start)
        eps = episodes_from(sig)
        print(f"\n═══ 双冰点(量5日均分位≤{th} & 涨停5日均分位≤{th}) — 事件 {len(eps)} 次 ═══")
        rows = []
        for d0 in eps:
            rows.append({
                "事件首日": d0,
                "当日成交(亿)": round(ser.loc[d0, "amt"]),
                "涨停触及": int(ser.loc[d0, "limit_touch"]),
                "等权T+5": fwd_ret(ser, d0, "eqw", 5), "等权T+10": fwd_ret(ser, d0, "eqw", 10),
                "等权T+20": fwd_ret(ser, d0, "eqw", 20), "等权T+60": fwd_ret(ser, d0, "eqw", 60),
                "上证T+20": fwd_ret(ser, d0, "sh", 20),
            })
        df = pd.DataFrame(rows)
        if df.empty:
            print("（无事件）")
            continue
        print(df.to_string(index=False))
        for h in ("等权T+5", "等权T+10", "等权T+20", "等权T+60"):
            v = df[h].dropna()
            if len(v):
                print(f"  {h}: 均值{v.mean():+.2f}% 中位{v.median():+.2f}% "
                      f"胜率{(v > 0).mean() * 100:.0f}% (n={len(v)})")
        # 基准对照：全期任意日的同窗收益(判断是不是"随便哪天买都涨")
        if th == 10:
            for h, lbl in ((5, "T+5"), (20, "T+20"), (60, "T+60")):
                alls = [(ser["eqw_cum"].iloc[i + h] / ser["eqw_cum"].iloc[i] - 1) * 100
                        for i in range(len(ser) - h) if ser.index[i] >= start]
                print(f"  [基准]全期任意日等权{lbl}: 均值{np.mean(alls):+.2f}% 中位{np.median(alls):+.2f}%")

    # 变体B：量冰点 + 广度≤25%
    sigB = (p_amt <= 10) & (ser["breadth5"] <= 25) & (ser.index >= start)
    epsB = episodes_from(sigB)
    print(f"\n═══ 变体B(量分位≤10 & 5日线上占比≤25%) — 事件 {len(epsB)} 次 ═══")
    for d0 in epsB:
        print(f"  {d0}  等权T+5 {fwd_ret(ser, d0, 'eqw', 5)}%  T+20 {fwd_ret(ser, d0, 'eqw', 20)}%  "
              f"T+60 {fwd_ret(ser, d0, 'eqw', 60)}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20230601")
    ap.add_argument("--end", default="20260805")
    a = ap.parse_args()
    run(a.start, a.end)
