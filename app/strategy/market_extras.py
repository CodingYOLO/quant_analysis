"""
扩展量化数据（基于 Tushare，已验证可用）：
  - get_limit_analysis : 涨停板专项（炸板率/连板分布/封单额）
  - get_dragon_tiger   : 龙虎榜个股席位分析（游资/机构/散户/北向 标注）
  - get_margin_sentiment: 两融余额 + 环比（杠杆资金情绪）
  - get_forecast_risk  : 业绩预告风险（预亏/预减 避雷）
  - get_holder_reduce  : 股东减持（结构化避雷）

所有数据走 Tushare 官方接口，确保准确。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.nodes.quick_report import _recent_trade_dates

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 营业部席位分类（游资/机构/散户/北向/量化）
# ──────────────────────────────────────────────

# 知名游资席位 → (江湖称号, 操作风格)。风格为经验性规律，非绝对，仅作参考。
# 注：游资席位会易主/变迁，此为公开市场长期认知。
# 关键词用"独特营业部地点"，规避"股份有限公司"等中间词导致的匹配失败
_FAMOUS_SEATS = [
    ("溧阳路", "章盟主", "大资金抱团做中线龙头，持仓偏久，标的稳定性较好"),
    ("绍兴", "赵老哥", "顶级打板游资，快准狠，次日易高溢价但需快进快出"),
    ("宛平南路", "炒股养家", "情绪周期大师，做强势股，看大盘情绪强弱"),
    ("成都北一环路", "成都帮", "凶悍接力打板，短线高波动"),
    ("北一环路", "成都帮", "凶悍接力打板，短线高波动"),
    ("宁波桑田路", "宁波敢死队", "老牌打板游资，接力为主"),
    ("宁波解放南路", "宁波敢死队", "老牌打板游资，接力为主"),
    ("杭州上塘路", "浙系游资", "打板接力，浙江帮风格"),
    ("益田路荣超", "深圳孙哥", "中大资金，做趋势强势股"),
    ("深圳福华一路", "深圳系", "深圳本地游资，打板接力"),
    ("深圳蛇口", "深圳系", "深圳本地游资，打板接力"),
    ("泰然九路", "深圳泰然帮", "老牌深圳游资席位"),
]


def classify_seat(exalter: str) -> tuple[str, str, str]:
    """
    营业部席位分类。
    返回 (类型, 标签, 风格提示)：类型∈{游资,机构,北向,散户,量化,营业部}
    风格提示为经验性参考（非绝对，席位会变迁）。
    """
    s = str(exalter or "")
    if "机构专用" in s:
        return "机构", "🏛️机构", "价值/趋势资金，持仓久，净买入=中线偏稳信号"
    if "股通专用" in s or "沪股通" in s or "深股通" in s:
        return "北向", "🌏北向", "外资偏蓝筹价值，相对稳，非短线爆发"
    if "量化" in s:
        return "量化", "🤖量化", "高频不做方向，次日可能有抛压"
    for kw, nick, style in _FAMOUS_SEATS:
        if kw in s:
            return "游资", f"🔥游资·{nick}", style
    # 拉萨/西藏系（东财散户聚集）
    if ("拉萨" in s or "西藏" in s) and ("东方财富" in s or "东财" in s):
        return "散户", "👥散户(东财拉萨系)", "散户跟风大军，人气标志但不稳定，警惕见顶"
    if "拉萨" in s or "西藏" in s:
        return "散户", "👥散户(拉萨系)", "散户/跟风资金，持续性弱"
    return "营业部", "💼游资/营业部", "普通游资/营业部席位"


# ──────────────────────────────────────────────
# 1. 涨停板专项
# ──────────────────────────────────────────────

def get_limit_analysis(date: str, provider: CompositeProvider | None = None) -> dict:
    """
    涨停板专项分析（官方 limit_list_d）。
    返回：涨停数/跌停数/炸板数/炸板率/连板分布/最高连板/封单额Top。
    """
    provider = provider or CompositeProvider()
    pro = provider._ts._api  # TushareProvider 内部 pro_api
    out: dict = {}
    try:
        up = pro.limit_list_d(trade_date=date, limit_type="U")
        down = pro.limit_list_d(trade_date=date, limit_type="D")
        zhaban = pro.limit_list_d(trade_date=date, limit_type="Z")  # 炸板
    except Exception as e:
        logger.warning("[limit] 涨停板数据失败: %s", e)
        return out

    n_up = len(up) if up is not None else 0
    n_down = len(down) if down is not None else 0
    n_zb = len(zhaban) if zhaban is not None else 0
    # 炸板率 = 炸板数 / (涨停数 + 炸板数)
    zhaban_rate = round(n_zb / max(n_up + n_zb, 1) * 100, 1)

    out["limit_up"] = n_up
    out["limit_down"] = n_down
    out["zhaban"] = n_zb
    out["zhaban_rate"] = zhaban_rate

    if up is not None and not up.empty and "limit_times" in up.columns:
        lt = pd.to_numeric(up["limit_times"], errors="coerce").fillna(1)
        dist = {}
        for v in lt:
            v = int(v)
            if v >= 2:
                dist[v] = dist.get(v, 0) + 1
        out["max_lianban"] = int(lt.max()) if len(lt) else 0
        out["lianban_dist"] = dict(sorted(dist.items(), reverse=True))
        # 最高连板代表股
        top = up[lt == lt.max()] if len(lt) else up.head(0)
        out["top_lianban_stocks"] = [
            f"{r['name']}({r['ts_code'].split('.')[0]})" for _, r in top.head(5).iterrows()
        ]
        # 封单额 Top5（封板资金最强=人气最高）
        if "fd_amount" in up.columns:
            up2 = up.copy()
            up2["_fd"] = pd.to_numeric(up2["fd_amount"], errors="coerce")
            top_fd = up2.nlargest(5, "_fd")
            out["top_seal"] = [
                {"name": r["name"], "code": r["ts_code"].split(".")[0],
                 "fd_yi": round(float(r["_fd"]) / 1e8, 2) if pd.notna(r["_fd"]) else 0,
                 "limit_times": int(r["limit_times"]) if pd.notna(r["limit_times"]) else 1}
                for _, r in top_fd.iterrows()
            ]
    return out


# ──────────────────────────────────────────────
# 2. 龙虎榜个股席位分析
# ──────────────────────────────────────────────

def get_dragon_tiger(date: str, provider: CompositeProvider | None = None) -> dict[str, dict]:
    """
    龙虎榜个股席位分析。
    返回 {ts_code: {net_buy_yi, seats:[{seat,tag,net_yi}], summary}}。
    summary 概括该股是游资主导/机构主导/散户/北向。
    """
    provider = provider or CompositeProvider()
    pro = provider._ts._api
    result: dict[str, dict] = {}
    try:
        ti = pro.top_inst(trade_date=date)
    except Exception as e:
        logger.warning("[lhb] 龙虎榜明细失败: %s", e)
        return result
    if ti is None or ti.empty:
        return result

    # 各类型主导时的次日风格提示
    _DOMINANT_HINT = {
        "机构": "机构主导→中线偏稳，次日溢价温和但持续性好",
        "北向": "北向主导→外资价值偏好，相对稳健",
        "游资": "游资主导→短线博弈，次日易高波动，快进快出",
        "量化": "量化主导→次日或有抛压",
        "散户": "散户主导→跟风为主，持续性弱，警惕见顶",
        "营业部": "营业部资金主导→关注次日承接",
    }
    for ts_code, g in ti.groupby("ts_code"):
        seats = []
        type_net: dict[str, float] = {}
        for _, r in g.iterrows():
            stype, tag, style = classify_seat(r.get("exalter", ""))
            net = (float(r.get("net_buy", 0)) or 0) / 1e8
            seats.append({"seat": str(r.get("exalter", ""))[:20], "tag": tag,
                          "net_yi": round(net, 2), "style": style})
            type_net[stype] = type_net.get(stype, 0) + net
        seats.sort(key=lambda x: abs(x["net_yi"]), reverse=True)
        total_net = round(sum(s["net_yi"] for s in seats), 2)
        # 主导力量：净买额绝对值最大的类型
        dominant = max(type_net.items(), key=lambda kv: abs(kv[1]))[0] if type_net else "营业部"
        # 知名游资昵称（若上榜）
        famous = [s["tag"] for s in seats if "游资·" in s["tag"]][:2]
        famous_s = "，含" + "、".join(t.replace("🔥游资·", "") for t in famous) if famous else ""
        result[ts_code] = {
            "net_buy_yi": total_net,
            "seats": seats[:4],
            "dominant": dominant,
            "summary": f"{dominant}主导（净{total_net:+.1f}亿{famous_s}）｜{_DOMINANT_HINT.get(dominant, '')}",
        }
    return result


# ──────────────────────────────────────────────
# 3. 两融余额（杠杆情绪）
# ──────────────────────────────────────────────

def get_margin_sentiment(date: str, provider: CompositeProvider | None = None) -> dict:
    """
    两融余额 + 环比。两融数据 T+1 公布，当日通常无，故取最近可用日(向前回溯)。
    rzye=融资余额（元）。
    """
    provider = provider or CompositeProvider()
    pro = provider._ts._api
    try:
        dates = _recent_trade_dates(provider, date, n=4)  # 含今日的最近4个交易日(升序)
    except Exception:
        dates = [date]
    # 从最近往前找有数据的两个交易日
    frames = []
    for d in reversed(dates):
        try:
            m = pro.margin(trade_date=d)
        except Exception:
            m = None
        if m is not None and not m.empty:
            frames.append((d, pd.to_numeric(m["rzye"], errors="coerce").sum() / 1e8))
        if len(frames) >= 2:
            break
    if not frames:
        return {}
    cur_date, rzye = frames[0]
    rzye_prev = frames[1][1] if len(frames) >= 2 else rzye
    return {
        "as_of": cur_date,
        "rzye_yi": round(float(rzye), 0),
        "rzye_chg_yi": round(float(rzye - rzye_prev), 0),
        "trend": "加杠杆🔴" if rzye > rzye_prev else "去杠杆🟢",
    }


# ──────────────────────────────────────────────
# 4. 业绩预告风险 + 5. 股东减持（避雷）
# ──────────────────────────────────────────────

_BAD_FORECAST = {"预减", "预亏", "首亏", "续亏", "略减"}


def get_forecast_risk(date: str, provider: CompositeProvider | None = None,
                      lookback_days: int = 10) -> dict[str, str]:
    """近期业绩预告中的负面类型（预亏/预减），返回 {ts_code: 类型}。"""
    provider = provider or CompositeProvider()
    pro = provider._ts._api
    out: dict[str, str] = {}
    try:
        dates = _recent_trade_dates(provider, date, n=lookback_days)
        for d in dates:
            df = pro.forecast(ann_date=d)
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                t = str(r.get("type", ""))
                if t in _BAD_FORECAST:
                    out[r["ts_code"]] = t
    except Exception as e:
        logger.debug("[forecast] 业绩预告失败: %s", e)
    return out


def get_holder_reduce(date: str, provider: CompositeProvider | None = None,
                      lookback_days: int = 10) -> dict[str, str]:
    """近期股东减持公告（结构化），返回 {ts_code: '减持X万股'}。"""
    provider = provider or CompositeProvider()
    pro = provider._ts._api
    out: dict[str, str] = {}
    try:
        dates = _recent_trade_dates(provider, date, n=lookback_days)
        for d in dates:
            df = pro.stk_holdertrade(ann_date=d)
            if df is None or df.empty:
                continue
            de = df[df["in_de"] == "DE"]  # 减持
            for _, r in de.iterrows():
                vol = r.get("change_vol")
                tag = f"减持{abs(float(vol))/1e4:.0f}万股" if pd.notna(vol) else "股东减持"
                out[r["ts_code"]] = tag
    except Exception as e:
        logger.debug("[holder] 减持数据失败: %s", e)
    return out
