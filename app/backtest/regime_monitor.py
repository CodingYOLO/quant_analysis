"""市场制度哨兵：监控「当日涨跌%」对未来收益的 IC 符号 —— 动量 vs 反转 制度。

924 后市场从「追涨续涨(动量)」翻成「涨多回落(反转·追高被套)」，即 当日涨跌% 的前瞻 IC 由正转负。
本哨兵用**近窗口短周期 IC**（比因子归因的1年更灵敏）追踪该符号，**制度翻转即推 Bark**——
让"追当日强势"类逻辑在制度回到动量/继续反转时第一时间知道，配紧的失效监控。

注：IC 用未来 horizon 日收益，天然滞后 horizon 日（只能算 ≥horizon 天前的截面）。制度本身慢变，可接受。
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

WINDOW = 60          # 近 N 个截面取 IC 均值（约3月·稳中带敏）
HORIZON = 10         # 前瞻交易日（对齐因子归因的 H10·反转是那个横上发现的）
FLIP_BAND = 0.02     # |IC| 超此才判定制度（带内=中性·避免噪声翻标签）
MIN_STOCKS = 100     # 单截面最少有效股数
MIN_CIRC_MV_YI = 50  # 流通市值下限(亿)·只看可交易的中大盘·与因子归因同宇宙(剔小盘反转噪声)


def _label(ic: float) -> str:
    if ic < -FLIP_BAND:
        return "🔴反转·追高被套"
    if ic > FLIP_BAND:
        return "🟢动量·追涨有效"
    return "⚪中性·无明显方向"


def compute_momentum_regime(end: str, provider: CompositeProvider | None = None,
                            window: int = WINDOW, horizon: int = HORIZON) -> dict:
    """近 window 截面「当日涨跌% → 前瞻horizon日收益」的 Rank-IC 均值 + 制度标签。

    当日涨跌用复权收盘日比价（≈pct_chg·已除权·免另取）；IC 用 .rank().corr()（无 scipy）。
    """
    from app.factors.breadth_qfq import build_qfq_panel
    prov = provider or CompositeProvider()
    # 回看覆盖：近window重叠(判当下) + 约1年非重叠(可交易口径·对齐因子归因)
    panel = build_qfq_panel(end, prov, lookback=max(window, 260) + horizon + 15)
    if panel is None or panel.empty or panel.shape[1] < horizon + 3:
        return {"ok": False, "msg": "复权面板不足"}

    # 流通市值过滤：只看可交易中大盘·与因子归因同宇宙(剔除小盘反转噪声)
    try:
        db = prov.get_daily_basic(end)
        if db is not None and not db.empty and "circ_mv" in db.columns:
            liquid = set(db[pd.to_numeric(db["circ_mv"], errors="coerce") >= MIN_CIRC_MV_YI * 1e4]["ts_code"])
            if len(liquid) >= MIN_STOCKS:
                panel = panel[panel.index.isin(liquid)]
    except Exception as e:
        logger.debug("[制度哨兵] 流通市值过滤跳过: %s", e)

    cols = list(panel.columns)

    def ic_at(i: int):
        ret = panel[cols[i]] / panel[cols[i - 1]] - 1                 # 当日涨跌(复权)
        fwd = panel[cols[i + horizon]] / panel[cols[i]] - 1           # 前瞻收益
        df = pd.concat([ret, fwd], axis=1).dropna()
        if len(df) < MIN_STOCKS:
            return None
        ic = df.iloc[:, 0].rank().corr(df.iloc[:, 1].rank())
        return float(ic) if pd.notna(ic) else None

    overlap = [x for x in (ic_at(i) for i in range(1, len(cols) - horizon)) if x is not None]
    if not overlap:
        return {"ok": False, "msg": "有效截面不足"}
    recent = overlap[-window:]                                       # 近window重叠截面·响应快(判"当下")
    # 非重叠(step=horizon)·可交易口径·对齐因子归因(每horizon日换仓的真实体验)
    nonover = [x for x in (ic_at(i) for i in range(len(cols) - horizon - 1, 0, -horizon)) if x is not None]
    recent_ic = round(float(np.mean(recent)), 4)
    trade_ic = round(float(np.mean(nonover)), 4) if nonover else None
    return {"ok": True, "date": end, "mean_ic": recent_ic, "trade_ic": trade_ic,
            "n": len(recent), "n_trade": len(nonover), "horizon": horizon,
            "label": _label(recent_ic)}


def _log_path():
    d = get_settings().cache_dir / "regime_monitor"
    d.mkdir(parents=True, exist_ok=True)
    return d / "regime_log.json"


def _load_log() -> list[dict]:
    p = _log_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def run_regime_monitor(date: str, provider: CompositeProvider | None = None) -> dict:
    """算当前制度→落滚动日志→制度标签翻转则返回告警（当日涨跌 IC 变了符号=市场性质变了）。"""
    cur = compute_momentum_regime(date, provider)
    if not cur.get("ok"):
        return {"date": date, "ok": False, "alerts": [f"⚠️ 制度哨兵无法计算: {cur.get('msg')}"]}
    log = _load_log()
    prior = [r for r in log if r.get("date", "") < date and r.get("label")]
    alerts = []
    if prior:
        last = prior[-1]
        if last["label"] != cur["label"] and "中性" not in (last["label"] + cur["label"]):
            alerts.append(
                f"⚠️ 市场制度切换：{last['label']}({last['mean_ic']:+.3f}) → {cur['label']}({cur['mean_ic']:+.3f})"
                f"·「追当日强势」逻辑需重估")
    log = [r for r in log if r.get("date") != date] + [cur]
    log.sort(key=lambda r: r.get("date", ""))
    try:
        _log_path().write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("[制度哨兵] 日志写入失败: %s", e)
    return {"date": date, "ok": True, "regime": cur, "alerts": alerts}
