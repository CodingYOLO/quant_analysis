"""个股融资盘 / 杠杆拥挤度画像（"杀融资盘"风险提示）。

机制：融资盘=融资融券账户借钱加杠杆买的仓位·怕深跌。股价跌向维持担保比例 130% 平仓线 →
追保→券商强制市价卖出→卖压再压低股价→逼近下一批账户平仓线=**负反馈螺旋**。主动砸盘资金
利用这个放大器专砸**融资拥挤度高**的票引爆连环强平。

用法（诚实纪律）：两融数据 **T+1 滞后**·用**趋势**不用绝对值·杠杆拥挤=回调时的强平放大器·
**非买卖建议**（只描述现状+风险）。非两融标的股无此项风险。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

CROWD_HIGH = 5.0     # 拥挤度(融资余额/流通市值·%)偏高线
CROWD_JAM = 8.0      # 拥挤线
_WIN = 10            # 趋势/背离窗口(交易日)


def _ff(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _circ_mv_yuan(prov: CompositeProvider, ts_code: str, date: str) -> float:
    """流通市值(元)。daily_basic circ_mv 单位=万元 → ×1e4。失败→0。"""
    try:
        db = prov.get_daily_basic(date)
        r = db[db["ts_code"] == ts_code]
        return _ff(r.iloc[0]["circ_mv"]) * 1e4 if not r.empty else 0.0
    except Exception:
        return 0.0


def _price_amount(prov: CompositeProvider, ts_code: str, end: str, win: int):
    """近 win+ 日收盘涨跌% + 最新成交额(元)。daily amount 单位=千元 → ×1e3。"""
    try:
        start = (pd.Timestamp(end) - pd.Timedelta(days=(win + 12) * 1.6)).strftime("%Y%m%d")
        d = prov.get_stock_daily(ts_code, start, end)
        if d is None or d.empty:
            return None, 0.0
        d = d.sort_values("trade_date")
        c = pd.to_numeric(d["close"], errors="coerce")
        price_chg = round((c.iloc[-1] / c.iloc[-win - 1] - 1) * 100, 1) if len(c) > win else None
        amount_yuan = _ff(d.iloc[-1].get("amount")) * 1e3          # 千元→元
        return price_chg, amount_yuan
    except Exception as e:
        logger.debug("[融资盘] 价格取数失败 %s: %s", ts_code, e)
        return None, 0.0


def margin_profile(ts_code: str, provider: CompositeProvider | None = None,
                   win: int = _WIN) -> dict:
    """个股融资盘画像：拥挤度 + 趋势 + 价跌余额不降背离 + 去杠杆出清 + 融资余额近20日序列。"""
    prov = provider or CompositeProvider()
    try:
        md = prov.get_margin_detail(ts_code)
    except Exception as e:
        logger.debug("[融资盘] %s 取数失败: %s", ts_code, e)
        md = None
    if md is None or md.empty or "rzye" not in md.columns:
        return {"is_target": False,
                "note": "非两融标的·无融资盘（下跌无强制平仓放大器·此项风险不适用）"}

    md = md.sort_values("trade_date").reset_index(drop=True)
    rzye = pd.to_numeric(md["rzye"], errors="coerce")          # 融资余额(元)
    date = str(md.iloc[-1]["trade_date"])
    rzye_now = float(rzye.iloc[-1])

    circ = _circ_mv_yuan(prov, ts_code, date)
    crowd = round(rzye_now / circ * 100, 2) if circ else None   # 拥挤度%
    level = ("拥挤" if (crowd is not None and crowd >= CROWD_JAM)
             else "偏高" if (crowd is not None and crowd >= CROWD_HIGH) else "正常")
    rzye_chg = round((rzye.iloc[-1] / rzye.iloc[-win - 1] - 1) * 100, 1) if len(rzye) > win else None

    price_chg, amount = _price_amount(prov, ts_code, date, win)
    rzmre = _ff(md.iloc[-1].get("rzmre"))                       # 融资买入额(元)
    buy_ratio = round(rzmre / amount * 100, 1) if amount else None   # 融资买入占成交%

    warn = ""
    if price_chg is not None and rzye_chg is not None:
        if price_chg <= -3 and rzye_chg >= -1:
            warn = ("⚠️ 价跌+融资余额不降=杠杆盘死扛·延迟引爆的雷"
                    "（每根阴线都在逼近集中强平·等余额明显回落再谈抄底）")
        elif rzye_chg <= -5 and price_chg <= 0:
            warn = ("🟢 融资余额连续回落=去杠杆出清中"
                    "（配合缩量+不再创新低=杠杆洗净·反弹结构才健康）")

    tail = md.tail(20)
    return {
        "is_target": True, "date": date,
        "rzye_yi": round(rzye_now / 1e8, 1), "crowd": crowd, "level": level,
        "rzye_chg": rzye_chg, "win": win, "price_chg": price_chg, "buy_ratio": buy_ratio,
        "warn": warn,
        "series": [round(_ff(x) / 1e8, 1) for x in tail["rzye"]],
        "series_dates": [str(d)[4:6] + "-" + str(d)[6:] for d in tail["trade_date"]],
        "disclaimer": ("两融数据 T+1 滞后·用趋势非绝对值。拥挤度=融资余额/流通市值"
                       f"（>{CROWD_HIGH:.0f}%偏高·>{CROWD_JAM:.0f}%拥挤=回调时强平放大器更猛）。非买卖建议。"),
    }
