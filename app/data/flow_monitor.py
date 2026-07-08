"""资金口径哨兵 + 完整性校验：让 elg+lg(canonical) 与 东财dc(对照) 互为哨兵，任一源悄改口径/漏数即告警。

- **一致性**：每日算 elg+lg vs moneyflow_dc 全市场相关系数 + 方向一致率，落滚动日志；
  用**滚动基线**(过去20日均值−2σ) + **绝对下限0.75**双阈值告警（固定阈值在极端行情日会误报）。
- **完整性**：moneyflow / moneyflow_dc **各自与"自己昨日"**比行数（**沪深口径·剔北交所.BJ**·对齐全系统口径·
  dc沪深≈5649 vs mf≈5194·**禁跨源比**），掉 >1% 判数据不全/延迟。北证东财资金流偶发整批缺数不再误报。

盘后 21:00 cron 跑（dc 当日结算偏晚·需等其补全）。异常推 Bark。
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.data.moneyflow import main_net_wan

logger = logging.getLogger(__name__)

CORR_FLOOR = 0.75        # 相关系数绝对硬下限
ROLL_WINDOW = 20         # 滚动基线回看日
ROLL_SIGMA = 2.0         # 偏离基线的 σ 倍数
MIN_BASELINE = 10        # 滚动基线最少样本（不足只用硬下限）
COMPLETE_DROP = 0.01     # 行数掉幅告警阈值（各自比自己昨日）


def _log_path():
    d = get_settings().cache_dir / "flow_monitor"
    d.mkdir(parents=True, exist_ok=True)
    return d / "consistency_log.json"


def _load_log() -> list[dict]:
    p = _log_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _hs_rows(df) -> int:
    """沪深口径行数（剔北交所 .BJ）。全系统按沪深口径·北证东财资金流偶发缺数(整批 .BJ)不应误触发完整性告警。"""
    if df is None or getattr(df, "empty", True):
        return 0
    if "ts_code" not in getattr(df, "columns", []):
        return int(len(df))
    return int((~df["ts_code"].astype(str).str.endswith(".BJ")).sum())


def _compute(date: str, prov: CompositeProvider) -> dict:
    """当日 elg+lg vs 东财dc 的相关/方向一致 + 两源行数(沪深口径·剔北交所)。dc 缺失时相关置 None。"""
    mf = prov.get_money_flow(date)
    our = main_net_wan(mf) / 1e4                                   # 亿
    try:
        dc = prov._ts._api.moneyflow_dc(trade_date=date)
    except Exception as e:
        logger.debug("[资金哨兵] dc 拉取失败: %s", e)
        dc = None
    mf_rows = _hs_rows(mf)                                         # 沪深口径(剔.BJ)·避免北证东财缺数误报
    dc_rows = _hs_rows(dc)
    corr = dir_agree = None
    if dc is not None and not dc.empty and not our.empty:
        dcn = pd.to_numeric(dc.set_index("ts_code")["net_amount"], errors="coerce")
        common = [c for c in our.index if c in dcn.index]
        ov, dv = our.reindex(common).to_numpy(), dcn.reindex(common).to_numpy()
        m = ~np.isnan(ov) & ~np.isnan(dv)
        ov, dv = ov[m], dv[m]
        if len(ov) > 100:
            corr = round(float(np.corrcoef(ov, dv)[0, 1]), 4)
            dir_agree = round(float(((ov > 0) == (dv > 0)).mean()), 4)
    return {"date": date, "corr": corr, "dir_agree": dir_agree,
            "mf_rows": mf_rows, "dc_rows": dc_rows}


def _completeness_alerts(cur: dict, prior: list[dict]) -> list[str]:
    """各源与自己最近一条比行数（禁跨源）·掉 >1% 告警。"""
    if not prior:
        return []
    last = prior[-1]
    out = []
    for src, k in (("moneyflow", "mf_rows"), ("moneyflow_dc", "dc_rows")):
        if last.get(k) and cur.get(k) and cur[k] < last[k] * (1 - COMPLETE_DROP):
            out.append(f"⚠️ {src} 行数 {last[k]}→{cur[k]} 掉 {(1-cur[k]/last[k])*100:.1f}%（疑数据不全/延迟·建议重拉）")
    return out


def _consistency_alerts(cur: dict, prior: list[dict]) -> list[str]:
    """硬下限 + 滚动基线(−2σ)双阈值。"""
    c = cur.get("corr")
    if c is None:
        return ["⚠️ 无法计算 elg+lg vs 东财dc 相关（dc 缺失）·无法哨兵"]
    out = []
    if c < CORR_FLOOR:
        out.append(f"🔴 elg+lg vs 东财dc 相关={c:.3f} < 硬下限{CORR_FLOOR}（疑某一源改了口径！立即核查）")
    hist = [r["corr"] for r in prior[-ROLL_WINDOW:] if r.get("corr") is not None]
    if len(hist) >= MIN_BASELINE:
        mu, sd = float(np.mean(hist)), float(np.std(hist))
        if sd > 0 and c < mu - ROLL_SIGMA * sd:
            out.append(f"🟡 相关={c:.3f} 跌破滚动基线 {mu:.3f}−2σ={mu-ROLL_SIGMA*sd:.3f}（异常波动·留意）")
    return out


def run_flow_monitor(date: str, provider: CompositeProvider | None = None) -> dict:
    """算当日指标→评估(完整性+一致性)→落滚动日志→返回 {metrics, alerts, ok}。"""
    prov = provider or CompositeProvider()
    cur = _compute(date, prov)
    log = _load_log()
    prior = [r for r in log if r.get("date", "") < date]
    alerts = _completeness_alerts(cur, prior) + _consistency_alerts(cur, prior)
    # 落日志（幂等：同日覆盖）
    log = [r for r in log if r.get("date") != date] + [cur]
    log.sort(key=lambda r: r.get("date", ""))
    try:
        _log_path().write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("[资金哨兵] 日志写入失败: %s", e)
    return {"date": date, "metrics": cur, "alerts": alerts, "ok": not alerts}
