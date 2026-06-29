"""个股监管/停牌风险：停牌(事实·Tushare) + 异动核查风险(连板派生) + 监管函(博查新闻)。

诚实分层（铁律）：
- 停牌 = 交易所公告事实（suspend_d）。
- 异动核查风险 = 交易所"异常波动"规则的**风险提示**（连续涨停达阈值），**非确定一定停牌**。
- 监管函/问询函 = 博查新闻·带来源·需自行核对。
"""

from __future__ import annotations

import datetime
import logging

logger = logging.getLogger(__name__)

# 异常波动阈值（连板数）：主板非ST 3连板≈累计偏离+33%已触异动公告；高连板→严重异常波动/停牌核查
_HI, _MID = 5, 3            # 普通股：≥5连板=核查风险高 / ≥3连板=已达异动
_HI_ST, _MID_ST = 3, 2      # ST/退市整理：阈值更低

_SUSPEND_CACHE: dict = {"date": "", "codes": set()}    # 当日缓存"当前停牌"代码集


def anomaly_risk(consec_boards, *, is_st: bool = False) -> dict | None:
    """连板 → 异常波动/核查风险（交易所规则·风险提示·不预测一定停牌）。无风险返回 None。"""
    c = int(consec_boards or 0)
    hi, mid = (_HI_ST, _MID_ST) if is_st else (_HI, _MID)
    if c >= hi:
        return {"level": "high", "boards": c, "text": f"{c}连板·触严重异常波动·停牌核查风险高"}
    if c >= mid:
        return {"level": "warn", "boards": c, "text": f"{c}连板·已达异常波动·留意交易所核查/自查"}
    return None


def suspended_codes(provider) -> set:
    """当前处于停牌状态的代码集合（近~10日 suspend_d·每股取最新记录 S=停牌·当日缓存·O(1)查）。"""
    today = datetime.date.today().strftime("%Y%m%d")
    if _SUSPEND_CACHE["date"] == today:
        return _SUSPEND_CACHE["codes"]
    codes: set = set()
    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=14)
        df = provider.get_suspend(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        if df is not None and not df.empty and "suspend_type" in df.columns:
            df = df.sort_values("trade_date")
            latest = df.groupby("ts_code").tail(1)               # 每股最新一条
            codes = set(latest[latest["suspend_type"] == "S"]["ts_code"])
    except Exception as e:
        logger.debug("[监管] 停牌集合获取失败: %s", e)
    _SUSPEND_CACHE.update(date=today, codes=codes)
    return codes


def reg_news(ts_code: str, name: str) -> list[dict]:
    """博查搜监管函/问询函/停牌核查/风险警示新闻（单股·个股360用·带来源）。"""
    try:
        from app.data.web_search import BochaSearchClient
        client = BochaSearchClient()
        if not getattr(client, "enabled", True):
            return []
        res = client.search(f"{name} 问询函 关注函 监管函 停牌核查 风险警示 立案", count=8,
                            freshness="oneMonth")
    except Exception as e:
        logger.debug("[监管] 博查失败: %s", e)
        return []
    kw = ("问询", "关注函", "监管函", "停牌", "核查", "风险警示", "处罚", "立案", "违规", "自查")
    out = []
    for r in (res or []):
        blob = str(r.get("title", "")) + str(r.get("summary", "") or r.get("snippet", ""))
        if any(k in blob for k in kw):
            out.append({"title": r.get("title", ""), "url": r.get("url", ""),
                        "site": r.get("site", ""), "date": str(r.get("date", ""))[:10]})
    return out[:5]


def reg_flag(ts_code: str, name: str, consec_boards, provider) -> dict | None:
    """列表场景轻量监管标记（停牌 + 连板异动核查·不博查·供自选/持仓/盘前用）。无风险返回 None。"""
    is_st = "ST" in str(name).upper()
    if ts_code in suspended_codes(provider):
        return {"kind": "suspend", "level": "high", "text": "停牌中"}
    a = anomaly_risk(consec_boards, is_st=is_st)
    if a:
        return {"kind": "anomaly", "level": a["level"], "text": a["text"]}
    return None
