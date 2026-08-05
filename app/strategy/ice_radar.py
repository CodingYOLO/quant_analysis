"""❄️ 冰点雷达（大盘体检页）：情绪冰点 + 成交额冰点 → 布局区提示（2026-08-06 用户定档）。

背景：用户多次观察到"双冰点后暴力反弹"但不敢行动。解法=把证据算出来常驻页面：
当前各冰点维度读数 + 历史每一次冰点事件的后续走势（含亏损案例·不许只展示赢的）。

口径（scripts/backtest_double_ice.py 回测定稿·2026-08-06）：
  量冰点   = 全A成交额5日均 120日分位≤15 或 缩量比(5日均/60日均)≤0.78
             —— 必须自适应口径：924后成交中枢抬台阶·250日绝对分位在新常态失真
             （第一版250日口径漏掉2025年全部事件·实测教训）
  情绪冰点 = 涨停触及数(日涨幅≥9.7%家数)5日均 120日分位≤15（回测验证优于"5日线上占比"）
  双冰点   = (量分位≤15 且 涨停分位≤15) 或 (缩量比≤0.78 且 涨停分位≤25)

诚实分层（参考档·非信号档）：
  n=9·未跨完整牛熊周期；2023年单边熊里冰点连环钝化(20230721 T+60=-8%)——须配大盘
  月线结构一起看；兑现集中T+20~T+60·T+5常仍阴跌——是分批布局区不是精确买点。非买卖建议。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

_TH_PCT = 15.0        # 双冰点分位阈（回测口径C）
_TH_RATIO = 0.78      # 缩量比阈（回测口径D）
_TH_LIM_RELAX = 25.0  # 口径D的情绪放宽阈
_WIN_PCT = 120        # 分位窗（自适应中枢·非250）
_MIN_PCT = 100

# 历史事件表（回测既定事实·硬编码=可审计；未来新事件由 live 检测追加为"进行中"）
# 字段：日期 · 触发口径 · 等权全A T+5/T+20/T+60(%)
EPISODES: list[dict] = [
    {"date": "20230721", "trig": "双冰点", "t5": 1.29, "t20": -1.46, "t60": -8.05,
     "note": "⚠️单边熊中钝化·冰点后继续阴跌"},
    {"date": "20230811", "trig": "双冰点", "t5": -1.0, "t20": -0.29, "t60": 2.57, "note": ""},
    {"date": "20230911", "trig": "双冰点", "t5": -1.1, "t20": -2.48, "t60": 4.2, "note": ""},
    {"date": "20240704", "trig": "双冰点", "t5": 2.43, "t20": 3.96, "t60": 24.55, "note": ""},
    {"date": "20240813", "trig": "双冰点", "t5": -2.07, "t20": -2.73, "t60": 42.36,
     "note": "924前夜·T+5仍跌·T+60暴力"},
    {"date": "20250331", "trig": "双冰点", "t5": -10.61, "t20": -2.34, "t60": 11.01,
     "note": "T+5剧烈下探后反转"},
    {"date": "20250428", "trig": "缩量比", "t5": 6.63, "t20": 8.89, "t60": 21.71, "note": ""},
    {"date": "20250526", "trig": "双冰点", "t5": 1.19, "t20": 4.18, "t60": 22.11, "note": ""},
    {"date": "20260403", "trig": "缩量比", "t5": 5.42, "t20": 12.25, "t60": 4.92, "note": ""},
]


def ice_state(p_amt: float | None, ratio: float | None, p_lim: float | None) -> dict:
    """当前冰点状态（纯函数·可单测）。返回 {code,label,detail}。

    code: double(双冰点) / vol(仅量冰) / senti(仅情绪冰) / none。
    """
    if p_amt is None or ratio is None or p_lim is None:
        return {"code": "na", "label": "数据不足", "detail": ""}
    vol_ice = p_amt <= _TH_PCT or ratio <= _TH_RATIO
    senti_ice = p_lim <= _TH_PCT
    double = (p_amt <= _TH_PCT and p_lim <= _TH_PCT) or \
             (ratio <= _TH_RATIO and p_lim <= _TH_LIM_RELAX)
    if double:
        return {"code": "double", "label": "❄️ 双冰点成立",
                "detail": "历史上此区域为分批布局区(T+5常仍跌·兑现看T+20~60)·配月线结构判断是否单边熊"}
    if vol_ice and not senti_ice:
        return {"code": "vol", "label": "🧊 半冰点·量已冰",
                "detail": f"情绪未冰(涨停分位{p_lim:.0f})——缩量但情绪热·常见于结构性行情缩圈"}
    if senti_ice and not vol_ice:
        return {"code": "senti", "label": "🧊 半冰点·情绪已冰",
                "detail": f"量未冰(量分位{p_amt:.0f}·缩量比{ratio:.2f})——放量下跌期·别急着接"}
    return {"code": "none", "label": "非冰点区",
            "detail": f"量分位{p_amt:.0f}·缩量比{ratio:.2f}·涨停分位{p_lim:.0f}——距冰点线尚有距离"}


def _load_series(end: str, provider: CompositeProvider) -> pd.DataFrame:
    """日频聚合序列(amt亿/limit_touch)·增量维护 parquet（每天只补缺日·首建约190次读缓存）。"""
    from app.config import get_settings
    from app.macro.sync import trading_days
    f = get_settings().cache_dir / "ice_radar" / "series.parquet"
    f.parent.mkdir(parents=True, exist_ok=True)
    old = pd.DataFrame()
    if f.exists():
        try:
            old = pd.read_parquet(f)
        except Exception:
            old = pd.DataFrame()
    have = set(old["date"]) if not old.empty else set()
    start = (pd.Timestamp(end) - pd.Timedelta(days=320)).strftime("%Y%m%d")   # 120窗+60比+余量
    need = [d for d in trading_days(start, end) if d not in have]
    rows = []
    for d in need:
        try:
            df = provider.get_daily(d)
            if df is None or df.empty:
                continue
            px = pd.to_numeric(df["pct_chg"], errors="coerce")
            rows.append({"date": d,
                         "amt": float(pd.to_numeric(df["amount"], errors="coerce").sum() / 1e5),
                         "limit_touch": int((px >= 9.7).sum())})
        except Exception:
            logger.debug("[冰点雷达] %s 聚合失败·跳过", d)
    ser = pd.concat([old, pd.DataFrame(rows)], ignore_index=True) if rows else old
    if ser.empty:
        return ser
    ser = ser.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    try:
        ser.to_parquet(f)
    except Exception:
        pass
    return ser


def build_ice_radar(end: str, provider: CompositeProvider | None = None) -> dict:
    """冰点雷达数据：当前仪表 + 状态 + 历史事件表 + 近60日三序列(走势图用)。"""
    prov = provider or CompositeProvider()
    ser = _load_series(end, prov)
    if ser.empty or len(ser) < _MIN_PCT + 10:
        return {"ok": False, "msg": "序列不足(首建需历史日线缓存·稍后重试)"}
    amt5 = ser["amt"].rolling(5).mean()
    lim5 = ser["limit_touch"].rolling(5).mean()
    p_amt = (amt5.rolling(_WIN_PCT, min_periods=_MIN_PCT).rank(pct=True) * 100)
    p_lim = (lim5.rolling(_WIN_PCT, min_periods=_MIN_PCT).rank(pct=True) * 100)
    ratio = amt5 / ser["amt"].rolling(60).mean()

    cur = {
        "p_amt": round(float(p_amt.iloc[-1]), 1) if pd.notna(p_amt.iloc[-1]) else None,
        "ratio": round(float(ratio.iloc[-1]), 3) if pd.notna(ratio.iloc[-1]) else None,
        "p_lim": round(float(p_lim.iloc[-1]), 1) if pd.notna(p_lim.iloc[-1]) else None,
        "amt": round(float(ser["amt"].iloc[-1])),
        "limit_touch": int(ser["limit_touch"].iloc[-1]),
    }
    state = ice_state(cur["p_amt"], cur["ratio"], cur["p_lim"])

    # 进行中检测：最近10个交易日内出现过双冰点·且不在既定事件表附近 → 新事件进行中
    live = None
    tail = min(10, len(ser))
    for i in range(len(ser) - tail, len(ser)):
        st = ice_state(
            float(p_amt.iloc[i]) if pd.notna(p_amt.iloc[i]) else None,
            float(ratio.iloc[i]) if pd.notna(ratio.iloc[i]) else None,
            float(p_lim.iloc[i]) if pd.notna(p_lim.iloc[i]) else None)
        if st["code"] == "double":
            d0 = str(ser["date"].iloc[i])
            if all(abs((pd.Timestamp(d0) - pd.Timestamp(e["date"])).days) > 20 for e in EPISODES):
                live = {"date": d0, "trig": "进行中", "note": "❄️ 新事件·后续收益待兑现"}
            break

    n60 = min(60, len(ser))
    return {
        "ok": True, "end": str(ser["date"].iloc[-1]), "cur": cur, "state": state,
        "live": live, "episodes": EPISODES,
        "series": {
            "dates": [f"{d[4:6]}-{d[6:]}" for d in ser["date"].tail(n60)],
            "amt": [round(x) for x in ser["amt"].tail(n60)],
            "p_amt": [round(float(x), 1) if pd.notna(x) else None for x in p_amt.tail(n60)],
            "p_lim": [round(float(x), 1) if pd.notna(x) else None for x in p_lim.tail(n60)],
            "ratio": [round(float(x), 3) if pd.notna(x) else None for x in ratio.tail(n60)],
        },
        "note": ("口径(回测定稿20260806)：量冰点=成交额5日均120日分位≤15或缩量比(5/60日均)≤0.78"
                 "(自适应中枢·924后250日绝对分位失真)；情绪冰点=涨停触及数5日均120日分位≤15"
                 "(回测优于5日线上占比)。⚠️参考档·n=9·未跨完整周期；2023单边熊中冰点连环钝化"
                 "——须配大盘月线结构看；兑现集中T+20~60·T+5常仍阴跌=分批布局区非精确买点。"
                 "非买卖建议。"),
    }
