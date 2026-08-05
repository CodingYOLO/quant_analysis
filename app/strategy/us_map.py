"""🌉 美股→A股映射（/usmap）：昨夜美股 → 今日A股可能的热门板块（2026-08-05 用户定档）。

需求：A股科技板块的隔夜定价锚在美股——开盘前看一眼美股对应链条，判断今天该盯哪些板块。

设计三层（**关键差异：不是拍脑袋说"强相关"，联动强度是算出来的**）：
  ① 映射表   美股标的 → A股同花顺概念（概念名全部核对真实存在·2026-08-05）+ 传导逻辑
  ② 实证联动 美股T日涨跌 vs A股概念指数**T+1日**涨跌：近250日相关系数 + 美股大涨(≥2%)
             时A股次日跟涨率 + 样本数——弱联动的映射如实标"弱"，不吹
  ③ 背离检测 美股大涨但A股概念昨日没跟 → 补涨候选；美股大跌但A股扛住 → 独立行情

时间轴（页面显著标注·这是本页的命门）：
  美股 T 日收盘（北京时间 T+1 凌晨）→ 对应 A股 T+1 日（今天）。
  故联动计算一律用 **美股T日 vs A股T+1日**，绝不同日对齐（同日对齐=用未来数据·结果虚高）。

数据源（20260805 服务器实测 24/24 可得）：
  美股 akshare `stock_us_daily`（新浪·Tushare us_daily 无权限已实测）；
  A股概念指数 ths_daily（复用 sector_mtf._index_daily·已日缓存）。

诚实边界：联动系数是历史统计不是保证；美股映射对**科技/半导体链最强**、对消费类最弱
（KO→白酒不是产业链传导·只是全球消费风险偏好·系数低就该低）；描述档·非买卖建议。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

_CORR_WIN = 250          # 联动统计窗口（交易日·约一年）
_BIG_MOVE = 2.0          # "大涨/大跌"阈值(%)
_MIN_SAMPLE = 20         # 条件命中率的最小样本

# ── 映射表（投资逻辑梳理·概念名 20260805 逐一核对存在于同花顺概念库）──────────────
# sym: 美股代码 · cn: 中文名 · concepts: A股对应概念 · note: 传导逻辑一句话
CHAINS: list[dict] = [
    {"chain": "🖥 AI算力链", "logic": "全球AI资本开支 → 芯片/网络/服务器订单 → A股光模块·PCB·液冷·代工",
     "items": [
         {"sym": "NVDA", "cn": "英伟达", "concepts": ["英伟达概念", "共封装光学(CPO)", "算力租赁"],
          "note": "AI算力总龙头·业绩指引与产品节奏是全链定价锚",
          "stocks": "中际旭创·新易盛·天孚通信·工业富联"},
         {"sym": "AVGO", "cn": "博通", "concepts": ["共封装光学(CPO)", "PCB概念", "光纤概念"],
          "note": "ASIC+网络芯片·与光模块/PCB订单最直接",
          "stocks": "沪电股份·生益科技·中际旭创·太辰光"},
         {"sym": "AMD", "cn": "AMD", "concepts": ["芯片概念", "算力租赁"],
          "note": "GPU次龙头·国产GPU对标情绪",
          "stocks": "海光信息·寒武纪·景嘉微"},
         {"sym": "SMCI", "cn": "超微电脑", "concepts": ["液冷服务器", "算力租赁"],
          "note": "AI服务器整机·液冷方案渗透率风向",
          "stocks": "工业富联·浪潮信息·英维克(液冷)·高澜股份"},
         {"sym": "CRWV", "cn": "CoreWeave", "concepts": ["算力租赁", "液冷服务器"],
          "note": "云算力租赁纯标的·租赁价格与产能对A股IDC/算力租赁传导",
          "stocks": "润泽科技·奥飞数据·数据港"},
     ]},
    {"chain": "💾 存储链", "logic": "存储原厂涨价/减产 → DRAM/NAND现货价 → A股模组·主控·接口芯片"
              "盈利弹性；材料端(前驱体/电子特气)随原厂扩产受益。"
              "三大DRAM原厂中美光(MU)与海力士(SKHY·2026-07-10纳斯达克ADR上市)均已覆盖，"
              "闪迪/西部数据看NAND、希捷看HDD；三星仍无美股通道(如实留空不硬凑)。",
     "items": [
         {"sym": "MU", "cn": "美光", "concepts": ["存储芯片", "芯片概念"],
          "note": "DRAM+NAND原厂·三大原厂中唯一美股纯标的·存储周期最直接的价格与景气锚",
          "stocks": "兆易创新·江波龙·佰维存储·澜起科技·北京君正"},
         {"sym": "SKHY", "cn": "SK海力士", "concepts": ["存储芯片", "芯片概念"],
          "note": "全球DRAM第二+HBM龙头(供英伟达)·2026-07-10 纳斯达克ADR上市·"
                  "HBM景气是AI存储链最硬的锚",
          "stocks": "兆易创新·澜起科技(内存接口)·雅克科技(前驱体材料)·香农芯创(分销)"},
         {"sym": "SNDK", "cn": "闪迪", "concepts": ["存储芯片"],
          "note": "NAND纯标的·闪存价格弹性最大",
          "stocks": "江波龙·佰维存储·德明利(主控)·朗科科技"},
         {"sym": "WDC", "cn": "西部数据", "concepts": ["存储芯片"],
          "note": "NAND+HDD·与闪迪同源·企业级存储需求",
          "stocks": "江波龙·佰维存储·同有科技"},
         {"sym": "STX", "cn": "希捷", "concepts": ["存储芯片", "算力租赁"],
          "note": "HDD龙头·AI数据中心冷存储需求(近端AI叙事新增量)",
          "stocks": "同有科技·易华录(数据存储)"},
     ]},
    {"chain": "🏭 半导体制造/设备", "logic": "代工产能与设备开支 → A股晶圆制造·设备·材料国产替代",
     "items": [
         {"sym": "TSM", "cn": "台积电", "concepts": ["芯片概念", "第三代半导体"],
          "note": "全球代工龙头·月营收与资本开支指引",
          "stocks": "中芯国际·华虹公司·芯原股份"},
         {"sym": "ASML", "cn": "阿斯麦", "concepts": ["光刻机", "芯片概念"],
          "note": "光刻机垄断·设备开支周期风向标·国产光刻机情绪对标",
          "stocks": "北方华创·中微公司·芯源微·茂莱光学"},
         {"sym": "AMAT", "cn": "应用材料", "concepts": ["光刻机", "国家大基金持股"],
          "note": "设备开支景气·对标北方华创等国产设备",
          "stocks": "北方华创·中微公司·拓荆科技·万业企业"},
         {"sym": "INTC", "cn": "英特尔", "concepts": ["芯片概念", "MCU芯片"],
          "note": "IDM转型与代工进展·PC链景气",
          "stocks": "中芯国际·兆易创新·复旦微电"},
     ]},
    {"chain": "🤖 AI软件/应用", "logic": "海外AI应用商业化验证 → A股AI应用/AIGC的叙事与估值锚",
     "items": [
         {"sym": "PLTR", "cn": "Palantir", "concepts": ["AI应用", "AIGC概念"],
          "note": "AI软件商业化标杆·A股AI应用情绪的海外镜子",
          "stocks": "科大讯飞·金山办公·万兴科技·彩讯股份"},
         {"sym": "META", "cn": "Meta", "concepts": ["AI眼镜", "虚拟现实", "AI应用"],
          "note": "双重身份：①AI capex四大云厂之一(Llama+算力开支) ②XR硬件链主——"
                  "Quest与Ray-Ban智能眼镜由歌尔股份代工·是A股AI眼镜题材最硬的海外锚",
          "stocks": "歌尔股份(Quest/雷朋代工)·立讯精密·亿道信息(AI眼镜ODM)·创维数字·佳禾智能"},
         {"sym": "GOOGL", "cn": "谷歌", "concepts": ["AI应用", "人工智能"],
          "note": "大模型+云·AI叙事主线之一",
          "stocks": "科大讯飞·昆仑万维·三六零"},
         {"sym": "MSFT", "cn": "微软", "concepts": ["AI应用", "算力租赁"],
          "note": "Azure AI资本开支·全球AI投入的资金源头之一",
          "stocks": "金山办公·用友网络·广联达"},
         {"sym": "ORCL", "cn": "甲骨文", "concepts": ["算力租赁", "AI应用"],
          "note": "云与AI订单积压·算力需求侧验证",
          "stocks": "润泽科技·宝信软件·太极股份"},
     ]},
    {"chain": "📱 消费电子", "logic": "苹果链出货与新品周期 → A股果链组装/零部件订单",
     "items": [
         {"sym": "AAPL", "cn": "苹果", "concepts": ["苹果概念", "消费电子概念"],
          "note": "果链订单与新品节奏·A股组装/声学/光学直接受益",
          "stocks": "立讯精密·歌尔股份·蓝思科技·领益智造"},
         {"sym": "QCOM", "cn": "高通", "concepts": ["消费电子概念", "汽车芯片"],
          "note": "手机SoC+汽车芯片·安卓链景气",
          "stocks": "闻泰科技·卓胜微·韦尔股份"},
         {"sym": "ARM", "cn": "ARM", "concepts": ["芯片概念", "MCU芯片"],
          "note": "IP授权·端侧AI芯片架构风向",
          "stocks": "中科蓝讯·恒玄科技·全志科技·兆易创新"},
     ]},
    {"chain": "🔋 新能源车", "logic": "特斯拉产销与降本 → A股特斯拉链/锂电/汽车电子订单与情绪",
     "items": [
         {"sym": "TSLA", "cn": "特斯拉", "concepts": ["特斯拉概念", "锂电池概念", "汽车电子"],
          "note": "产销/FSD/储能三条线·A股特斯拉链弹性最直接",
          "stocks": "拓普集团·三花智控·旭升集团·宁德时代"},
     ]},
    {"chain": "🚀 商业航天", "logic": "SpaceX上市后板块重定价 → A股商业航天/卫星链情绪",
     "items": [
         {"sym": "SPCX", "cn": "SpaceX", "concepts": ["商业航天", "卫星导航"],
          "note": "2026-06-12 纳斯达克上市(发行价$135)·全球商业航天定价锚·A股对标情绪",
          "stocks": "中国卫星·航天电子·上海瀚讯·铖昌科技"},
         {"sym": "RKLB", "cn": "火箭实验室", "concepts": ["商业航天", "军工信息化"],
          "note": "小火箭发射服务·商业航天β",
          "stocks": "航天电子·航天彩虹·中国卫通"},
     ]},
    {"chain": "🥤 消费(弱映射)", "logic": "⚠️非产业链传导·只是全球消费股风险偏好的粗略参照——系数通常很低，如实呈现",
     "items": [
         {"sym": "KO", "cn": "可口可乐", "concepts": ["白酒概念", "乳业", "食品安全"],
          "note": "⚠️无供应链关系·仅作全球消费防御情绪参照·联动弱属正常",
          "stocks": "贵州茅台·伊利股份·海天味业(⚠️仅情绪参照·无供应链关系)"},
     ]},
]

ALL_SYMS: list[str] = [it["sym"] for ch in CHAINS for it in ch["items"]]


# ── 纯函数（可单测）────────────────────────────────────────────────────────

def link_stats(us_ret: pd.Series, cn_ret_next: pd.Series,
               big: float = _BIG_MOVE) -> dict:
    """美股T日涨跌 vs A股T+1日涨跌 的联动统计（索引须已按"美股T日"对齐）。

    返回 corr(相关系数) / hit_up(美股大涨时A股次日上涨率) / n_up(样本) / n(总样本)。
    样本不足按 None 返回——小样本命中率无意义（宁缺毋滥）。
    """
    df = pd.DataFrame({"us": us_ret, "cn": cn_ret_next}).dropna()
    n = len(df)
    if n < 30:
        return {"corr": None, "hit_up": None, "n_up": 0, "n": n}
    corr = float(df["us"].corr(df["cn"]))
    up = df[df["us"] >= big]
    hit = float((up["cn"] > 0).mean() * 100) if len(up) >= _MIN_SAMPLE else None
    # ⭐基准对照(诚实红线)：该A股概念**全样本**的上涨日比例——牛市里概念本身就常涨，
    # 不给基准的话 65% 跟涨率看着很高、实则可能毫无超额。edge = hit − base 才是真信息。
    base = float((df["cn"] > 0).mean() * 100)
    return {"corr": round(corr, 3) if pd.notna(corr) else None,
            "hit_up": round(hit, 1) if hit is not None else None,
            "base_up": round(base, 1),
            "edge": round(hit - base, 1) if hit is not None else None,
            "n_up": int(len(up)), "n": n}


def next_day_align(us_ret: pd.Series, cn_ret: pd.Series) -> pd.Series:
    """⭐时间轴命门：把 A股 T+1 日收益贴到"美股 T 日"的索引上（纯函数·可单测）。

    美股 T 日收盘在北京时间 T+1 凌晨 → 影响 A股 T+1 日。故 aligned[T] = cn_ret[T之后的
    第一个A股交易日]。中美假期不重合时该位为 NaN（由 link_stats 的 dropna 剔除·不脑补）。
    """
    cn_days = list(cn_ret.index)
    out = {}
    for d in us_ret.index:
        nxt = next((x for x in cn_days if x > d), None)   # 严格晚于美股T日的首个A股交易日
        out[d] = cn_ret.get(nxt) if nxt is not None else np.nan
    return pd.Series(out, dtype=float)


def divergence(us_chg: float | None, cn_chg: float | None,
               big: float = _BIG_MOVE) -> dict:
    """背离标记：美股大涨A股没跟=补涨候选；美股大跌A股扛住=独立。纯函数。"""
    if us_chg is None or cn_chg is None:
        return {"code": "", "label": ""}
    if us_chg >= big and cn_chg <= 0:
        return {"code": "catchup", "label": "🔥美股大涨·A股昨日未跟"}
    if us_chg <= -big and cn_chg >= 0:
        return {"code": "resist", "label": "🛡A股抗跌·独立于外盘"}
    if us_chg >= big and cn_chg > 0:
        return {"code": "sync", "label": "✅内外共振"}
    if us_chg <= -big and cn_chg < 0:
        return {"code": "drag", "label": "⚠️内外同步走弱"}
    return {"code": "", "label": ""}


# ── 数据获取 ───────────────────────────────────────────────────────────────

def _us_daily(prov: CompositeProvider, sym: str) -> pd.DataFrame:
    """单只美股日线（走 provider·架构原则：策略层不直连数据源）。失败不阻塞其余标的。"""
    try:
        return prov.get_us_daily(sym)
    except Exception as e:
        logger.warning("[美股映射] %s 取数失败: %s", sym, e)
        return pd.DataFrame()


def _concept_daily(prov: CompositeProvider, name: str, code_map: dict, end: str) -> pd.DataFrame:
    """A股概念指数日线（复用 sector_mtf 的缓存·零新增调用）。"""
    from app.strategy.sector_mtf import _index_daily
    code = code_map.get(name)
    if not code:
        return pd.DataFrame()
    try:
        return _index_daily(prov, "concept", code, end)
    except Exception:
        return pd.DataFrame()


def build_us_map(end: str, provider: CompositeProvider | None = None,
                 force: bool = False) -> dict:
    """美股→A股映射面板（日缓存）。end=A股最新交易日。"""
    import json

    from app.config import get_settings
    prov = provider or CompositeProvider()
    cdir = get_settings().cache_dir / "us_map"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{end}_v1.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    from app.strategy.sector_mtf import _concept_code_map
    code_map = _concept_code_map(prov, end)

    # A股概念收益序列（逐概念一次·同一概念多标的共用）
    need = sorted({c for ch in CHAINS for it in ch["items"] for c in it["concepts"]})
    cn_ret: dict[str, pd.Series] = {}
    cn_last: dict[str, dict] = {}
    for nm in need:
        k = _concept_daily(prov, nm, code_map, end)
        if k is None or k.empty or len(k) < 60:
            continue
        k = k.sort_values("trade_date")
        c = pd.to_numeric(k["close"], errors="coerce")
        r = (c.pct_change() * 100).round(3)
        r.index = k["trade_date"].astype(str).values
        cn_ret[nm] = r
        cn_last[nm] = {
            "chg": round(float(r.iloc[-1]), 2) if pd.notna(r.iloc[-1]) else None,
            "chg5": round(float((c.iloc[-1] / c.iloc[-6] - 1) * 100), 2) if len(c) > 6 else None,
            "date": str(k["trade_date"].iloc[-1]),
        }

    chains_out, us_date = [], ""
    for ch in CHAINS:
        items = []
        for it in ch["items"]:
            us = _us_daily(prov, it["sym"])
            if us is None or us.empty:
                items.append({**{k: it[k] for k in ("sym", "cn", "note")},
                              "stocks": it.get("stocks", ""), "state": "na", "concepts": []})
                continue
            us = us.sort_values("date").reset_index(drop=True)
            uc = pd.to_numeric(us["close"], errors="coerce")
            us_ret = (uc.pct_change() * 100).round(3)
            us_ret.index = us["date"].astype(str).values
            us_date = max(us_date, str(us["date"].iloc[-1]))
            row = {
                **{k: it[k] for k in ("sym", "cn", "note")},
                "stocks": it.get("stocks", ""), "state": "ok",
                "us_date": str(us["date"].iloc[-1]),
                "close": round(float(uc.iloc[-1]), 2),
                "chg": round(float(us_ret.iloc[-1]), 2) if pd.notna(us_ret.iloc[-1]) else None,
                "chg5": round(float((uc.iloc[-1] / uc.iloc[-6] - 1) * 100), 2) if len(uc) > 6 else None,
                "spark": [round(float(x), 2) for x in uc.tail(20)],
                "concepts": [],
            }
            for nm in it["concepts"]:
                r = cn_ret.get(nm)
                cinfo = {"name": nm, "corr": None, "hit_up": None,
                         "base_up": None, "edge": None, "n": 0,
                         "cn_chg": (cn_last.get(nm) or {}).get("chg"),
                         "cn_chg5": (cn_last.get(nm) or {}).get("chg5")}
                if r is not None:
                    aligned = next_day_align(us_ret, r)
                    st = link_stats(us_ret.tail(_CORR_WIN), aligned.tail(_CORR_WIN))
                    cinfo.update(st)
                cinfo["div"] = divergence(row["chg"], cinfo["cn_chg"])
                row["concepts"].append(cinfo)
            items.append(row)
        chains_out.append({"chain": ch["chain"], "logic": ch["logic"], "items": items})

    out = {
        "ok": True, "end": end, "us_date": us_date,
        "chains": chains_out,
        "note": ("时间轴：美股 T 日收盘(北京时间 T+1 凌晨) → 对应 A股 T+1 日；联动统计一律"
                 "**美股T日 vs A股T+1日**(绝不同日对齐)。联动=近250日相关系数；跟涨率=美股单日"
                 "≥+2%时A股概念**次日**上涨的历史比例(样本<20不显示)。"
                 "⚠️历史统计非保证·科技/半导体链联动最强、消费类最弱(KO→白酒非产业链传导·"
                 "系数低属正常·如实呈现不吹)。数据源：美股akshare新浪(服务器实测)·"
                 "A股概念指数同花顺。描述档·非买卖建议。"),
    }
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out
