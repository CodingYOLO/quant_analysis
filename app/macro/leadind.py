"""领先指标页（/leadind）：行业领先指标的产业链分组面板。

设计（2026-08-03 用户定档：链条式体系化思维·看了就知道影响什么板块甚至个股）：
- 数据管道 100% 复用宏观底盘：registry(IND层) → FuturesMainAdapter → macro_daily →
  compute(分位/z/异动V3) → 本模块只做**产业链语义层**（分组/传导方向/代表股/换月修正）；
- 每条指标 = 完整传导语句：现值 + 日变化 + 一年分位 + 「涨→利好谁·利空谁」+ 代表股；
- 金融流动性链直接复用宏观页既有指标（同库同数·不产生第二套口径）；
- 换月修正：主力连续换月日的 chg 被新旧合约基差污染——用 fut_mapping 识别换月日，
  以新主力自身 pre_close 重算当日涨跌幅（20260720 CU 实测 pre_close 换月感知✓）。
全部**描述档·传导逻辑为推演未回测·非买卖建议**。代表股为链条示例·非推荐。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.macro import store
from app.macro.adapters.ts_fut import FUT_CODES

logger = logging.getLogger(__name__)

# ── 产业链分组（页面从上到下的顺序）───────────────────────────────────────────
# 每链：label · 一句链条逻辑(教链式思维) · 指标列表(顺链条上游→下游排)
CHAINS: list[dict] = [
    {"label": "🐷 养殖链", "codes": ["fut_lh", "fut_c", "fut_m"],
     "logic": "链条：饲料成本(玉米/豆粕) → 养殖利润(猪价−成本) → 补栏/去化 → 下一轮周期。"
              "猪涨料跌=利润双击；三者一起涨=通胀交易。"},
    {"label": "🏗 地产黑色链", "codes": ["fut_rb", "fut_i", "fut_fg", "fut_v"],
     "logic": "链条：开工(螺纹/PVC) → 竣工(玻璃) 两端分开看；铁矿是螺纹的成本端。"
              "螺纹涨矿不涨=钢厂利润走阔；玻璃独涨=竣工逻辑(家电家居后周期跟随)。"},
    {"label": "⚡ 新能源链", "codes": ["fut_lc", "fut_sa"],
     "logic": "链条：碳酸锂→电池→整车(方向上下游相反：锂涨利好资源利空中游)；"
              "纯碱→光伏玻璃→装机(纯碱与玻璃背离时=光伏逻辑而非地产逻辑)。多晶硅暂无稳定数据源。"},
    {"label": "🥇 有色贵金属", "codes": ["fut_cu", "fut_al", "fut_au", "fut_ag"],
     "logic": "铜铝看全球需求(工业属性)，金银看避险与实际利率(金融属性)；"
              "金银比走低(银补涨)=贵金属行情后段信号。"},
    {"label": "⛽ 能源化工", "codes": ["fut_sc", "fut_ur", "fut_sp"],
     "logic": "原油是化工链总成本锚：油涨→上游开采/油服受益·下游(航空/物流/化纤)成本承压。"
              "尿素看农需季节·纸浆看造纸成本。"},
    {"label": "🚢 航运出口", "codes": ["fut_ec"],
     "logic": "集运欧线期货=运价的远期定价·航运景气最快日频信号；与出口数据相互印证。"},
    {"label": "🌦 天气敏感农产品", "codes": ["fut_sr", "fut_p", "fut_cf", "fut_ap"],
     "logic": "厄尔尼诺/拉尼娜等气候异常最终都打进这些价格(期货=天气信息的日频聚合)：糖(印泰干旱)、"
              "棕榈油(东南亚)、棉花(新疆/美棉)、苹果(花期冻害)。天气题材热而这些价格不动=炒作虚。"
              "ONI厄尔尼诺指数待接入(境外源需服务器实测)。"},
    {"label": "💰 金融流动性(复用宏观页)", "codes": ["turnover_total", "margin_ratio", "cn_10y"],
     "logic": "成交额与两融看券商景气与市场热度；10Y利率是银行保险资产端与成长股估值的分母。"
              "与宏观传导页同库同数·此处只做行业视角摘录。"},
]

# ── 传导映射：涨→利好谁·利空谁 + 代表股（示例非推荐）──────────────────────────
IMPACT: dict[str, dict] = {
    "fut_lh": {"up_good": "养殖(牧原股份/温氏股份/新希望)", "up_bad": "屠宰及肉制品成本端(双汇发展)"},
    "fut_c": {"up_good": "种植(北大荒/苏垦农发)", "up_bad": "养殖/饲料成本端(牧原/海大集团)"},
    "fut_m": {"up_good": "大豆贸易/压榨环节", "up_bad": "养殖饲料成本端(生猪/白鸡养殖)"},
    "fut_rb": {"up_good": "钢铁(宝钢股份/华菱钢铁)·稳增长预期", "up_bad": "下游建筑/制造用钢成本"},
    "fut_i": {"up_good": "铁矿资源(海南矿业)", "up_bad": "钢厂毛利(矿强钢弱=利润挤压)"},
    "fut_fg": {"up_good": "浮法玻璃(旗滨集团/南玻A)·竣工链信号(家电家居跟随)", "up_bad": "下游深加工成本"},
    "fut_sa": {"up_good": "纯碱(远兴能源/山东海化)·光伏玻璃链景气", "up_bad": "玻璃厂成本端"},
    "fut_lc": {"up_good": "锂矿(天齐锂业/赣锋锂业/中矿资源)", "up_bad": "电池/整车成本端(宁德时代/比亚迪)"},
    "fut_cu": {"up_good": "铜矿(紫金矿业/洛阳钼业/西部矿业)", "up_bad": "电网设备/家电用铜成本"},
    "fut_al": {"up_good": "电解铝(中国铝业/云铝股份/神火股份)", "up_bad": "铝加工/光伏边框成本"},
    "fut_au": {"up_good": "黄金股(山东黄金/中金黄金/赤峰黄金)", "up_bad": "金饰消费需求(周大生成本)"},
    "fut_ag": {"up_good": "白银资源(盛达资源/兴业银锡)", "up_bad": "光伏浆料成本端"},
    "fut_sc": {"up_good": "油气开采/油服(中国海油/中海油服/海油工程)", "up_bad": "航空(三大航燃油)/物流/化纤成本"},
    "fut_ur": {"up_good": "氮肥/煤化工(华鲁恒升/云天化)", "up_bad": "种植成本端"},
    "fut_v": {"up_good": "氯碱化工(中泰化学)", "up_bad": "下游管材型材成本"},
    "fut_sp": {"up_good": "自有浆一体化纸企(太阳纸业相对受益)", "up_bad": "外购浆纸企毛利(晨鸣纸业等)"},
    "fut_ec": {"up_good": "集运(中远海控/中远海能间接)", "up_bad": "跨境电商/外贸物流成本"},
    "fut_sr": {"up_good": "制糖(中粮糖业/粤桂股份)", "up_bad": "食品饮料糖成本端"},
    "fut_p": {"up_good": "棕榈油贸易/油脂加工", "up_bad": "食品用油成本端"},
    "fut_cf": {"up_good": "棉花种植/贸易(新农开发/新赛股份)", "up_bad": "纺织服装成本端(华孚时尚)"},
    "fut_ap": {"up_good": "果品贸易(样本少·主要用于验证天气炒作真伪)", "up_bad": "—"},
    # 金融流动性(复用宏观指标·行业视角)
    "turnover_total": {"up_good": "券商(东方财富/中信证券——经纪与两融弹性)", "up_bad": "—"},
    "margin_ratio": {"up_good": "券商两融业务·风险偏好回升信号", "up_bad": "高位=杠杆脆弱性积累"},
    "cn_10y": {"up_good": "银行保险资产端收益(招商银行/中国平安)", "up_bad": "长久期成长股估值分母"},
}


# ── 换月识别与当日涨跌幅修正 ─────────────────────────────────────────────────

def _roll_fix(end: str) -> dict:
    """{metric_code: {"roll": True, "chg_true": 按新主力自身昨收的当日涨跌幅%}}·按日缓存。

    只对**换月日当天**的品种出手；平日 compute 的 chg_1d 本来就是对的。
    识别口径：fut_mapping 最新两个交易日的映射合约不同 = end 为换月日。
    """
    from app.data.cache import cached_daily

    def _build():
        from app.data.composite_provider import CompositeProvider
        from app.data.cache import rate_limited_call
        pro = CompositeProvider()._ts._api
        start = (pd.Timestamp(end) - pd.Timedelta(days=14)).strftime("%Y%m%d")
        rows = []
        for code, ts_code in FUT_CODES.items():
            try:
                m = rate_limited_call("tushare_fut_mapping", pro.fut_mapping,
                                      ts_code=ts_code, start_date=start, end_date=end)
                if m is None or len(m) < 2:
                    continue
                m = m.sort_values("trade_date")
                if str(m["trade_date"].iloc[-1]) != end:
                    continue
                if m["mapping_ts_code"].iloc[-1] == m["mapping_ts_code"].iloc[-2]:
                    continue                                    # 未换月
                d = rate_limited_call("tushare_fut_daily", pro.fut_daily,
                                      ts_code=ts_code, start_date=end, end_date=end)
                if d is None or d.empty:
                    continue
                r = d.iloc[0]
                close, prec = pd.to_numeric(r.get("close")), pd.to_numeric(r.get("pre_close"))
                if pd.notna(close) and pd.notna(prec) and prec > 0:
                    rows.append({"code": code,
                                 "chg_true": round(float(close / prec - 1) * 100, 4)})
            except Exception as e:
                logger.debug("[领先指标] %s 换月检测失败: %s", code, e)
        return pd.DataFrame(rows if rows else [], columns=["code", "chg_true"])

    df = cached_daily("fut_roll_fix", end, _build)
    if df is None or df.empty:
        return {}
    return {r["code"]: {"roll": True, "chg_true": float(r["chg_true"])}
            for _, r in df.iterrows()}


# ── 面板构建 ────────────────────────────────────────────────────────────────

def build_leadind_panel(date: str | None = None) -> dict:
    """领先指标面板：⚡今日异动 + 产业链分组卡片。卡片构建复用宏观 service._build_card。"""
    from app.macro.service import _build_anomalies, _build_card, resolve_date
    store.init_db()
    d = resolve_date(date)
    if not d:
        return {"ok": False, "error": "宏观库为空：请先运行 macro-backfill"}
    metas = {m["code"]: m for m in store.get_meta(enabled_only=False)}
    day_rows = store.read_panel(d)
    rolls = _roll_fix(d) if d == store.latest_date() else {}

    chains = []
    for ch in CHAINS:
        cards = []
        for code in ch["codes"]:
            m = metas.get(code)
            if not m or not m["enabled"]:
                continue
            card = _build_card(m, day_rows.get(code), d)
            imp = IMPACT.get(code) or {}
            card["up_good"], card["up_bad"] = imp.get("up_good", ""), imp.get("up_bad", "")
            card["reused_macro"] = m["layer"] != store.IND_LAYER
            fix = rolls.get(code)
            if fix and card.get("state") == "ok":
                card["chg_1d"] = fix["chg_true"]     # 换月日：按新主力自身昨收(pre_close换月感知)
                card["roll_day"] = True
            cards.append(card)
        chains.append({"label": ch["label"], "logic": ch["logic"], "cards": cards})

    ind_metas = [m for m in metas.values() if m["layer"] == store.IND_LAYER and m["enabled"]]
    anomalies = _build_anomalies(ind_metas, day_rows)
    disabled = [{"name": m["name_cn"], "note": (m["note"] or "")[:160]}
                for m in metas.values()
                if m["layer"] == store.IND_LAYER and not m["enabled"]]
    return {
        "ok": True, "date": d, "is_lookback": bool(date) and d != store.latest_date(),
        "anomalies": anomalies, "chains": chains, "pending": disabled,
        "note": ("价格=期货主力连续收盘价(Tushare·上游为交易所官方结算数据·收盘后当日更新)；"
                 "换月日涨跌幅已按新主力自身昨收修正并标注。分位=近三年该价格自身分位(point-in-time)。"
                 "⚠️主力判定规则各家不同(Tushare偏成交量·部分行情软件按持仓量)——移仓期两者可能各认"
                 "相邻月份合约·远月升水大的品种(如生猪)价差可达数%；本页分位/异动全程同一口径自洽，"
                 "跨软件对照价格请认具体月份合约。传导方向为产业逻辑推演·未回测·代表股为链条示例·"
                 "全部非买卖建议。"),
    }


def build_leadind_ai(date: str | None = None, force: bool = False) -> dict:
    """领先指标 AI 全景研判（页首·日缓存）。

    输入纪律（沿用板块诊断 2026-08-03 教训：喂增量与结构·不喂裸排名）：
    ①各链分位全景(存量·但按"历史极值/中位"压缩) ②今日异动(增量·V3口径) ③跨链背离素材。
    输出锁死为结构描述——LLM 只做"把21个数拼成一段人话"，不做方向判断。
    """
    import json

    from app.config import get_settings
    d_panel = build_leadind_panel(date)
    if not d_panel.get("ok"):
        return {"ok": False, "error": d_panel.get("error", "面板为空")}
    d = d_panel["date"]
    cdir = get_settings().cache_dir / "leadind"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"ai_{d}_v1.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    lines = []
    for ch in d_panel["chains"]:
        seg = []
        for c in ch["cards"]:
            if c.get("state") != "ok":
                continue
            p, chg = c.get("pctile"), c.get("chg_1d")
            seg.append(f"{c['name'].replace('期货(主力)', '').replace('(主力)', '')}"
                       f"{'—' if chg is None else f'{chg:+.1f}%'}·分位{'—' if p is None else round(p)}"
                       f"{'·⚡异动' + ('↑' if c.get('anomaly', 0) > 0 else '↓') if c.get('anomaly') else ''}"
                       f"{'·换月日' if c.get('roll_day') else ''}")
        if seg:
            lines.append(f"{ch['label']}: " + "，".join(seg))
    anoms = [f"{a['name']}{'↑' if a['dir'] > 0 else '↓'}{a.get('chg_1d') or 0:+.2f}%"
             f"(分位{'' if a.get('pctile') is None else round(a['pctile'])})"
             for a in d_panel["anomalies"]]
    data = ("【各链现状（涨跌为当日·分位为该价格近三年自身分位0-100）】\n" + "\n".join(lines)
            + "\n\n【今日异动（超自身近一年95分位变动·连续2日同向确认）】\n"
            + ("；".join(anoms) if anoms else "（今日无异动）"))

    prompt = ("你是产业链价格研究员。下面是A股各行业领先指标(期货主力价)的当日全景。"
              "写一段全景研判，固定四段（保留【】标题·总计220-350字）：\n"
              "【一句话全景】哪几条链在历史极值区(分位≤10或≥90必须点名)、哪几条在中位·一句话说完。\n"
              "【今天什么变了】只讲异动清单里的品种及其链条含义；无异动就明说'今日各链价格无异动'。\n"
              "【背离与联动】跨链信息：同链内部或链与链之间的显著背离(如工业金属高位而黑色低位="
              "外需定价与内需定价的分裂)·只报数据支持的。\n"
              "【板块映射】把上述现状翻译成'哪些板块的成本端/收入端正处于什么状态'(结构口径·非荐股)。\n"
              "铁律：只用输入数据·品种名和数字不许编造；分位是历史位置不是预测；"
              "这是价格结构描述，不是买卖建议——全文禁止出现：建议/应该/加仓/减仓/仓位/观望/"
              "布局/持有/买入/卖出/回避/不宜/适合。\n\n" + data)
    try:
        from app.llm.client import LLMClient
        from app.llm.stance import ANALYST_STANCE
        raw = LLMClient().chat([{"role": "user", "content": ANALYST_STANCE + "\n\n" + prompt}],
                               task_type="pro", temperature=0.3, max_tokens=2000)
    except Exception as e:
        logger.warning("[领先指标AI] LLM 失败: %s", e)
        raw = ""
    out = {"ok": bool(raw), "date": d, "summary": (raw or "").strip(),
           "disclaimer": "AI 基于领先指标价格/分位/异动数据综合·价格结构描述·非买卖建议·不预测涨跌。"}
    if out["ok"]:
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out


def validate_leadind() -> list[str]:
    """一致性自检（单测调用）：链条引用的代码必须存在、IND 指标必须有传导映射且只进一条链。"""
    from app.macro import registry
    problems = []
    reg = {m.code: m for m in registry.METRICS}
    seen: dict[str, str] = {}
    for ch in CHAINS:
        for c in ch["codes"]:
            if c not in reg:
                problems.append(f"{ch['label']}: 引用了不存在的指标 {c}")
            if c in seen:
                problems.append(f"{c}: 同时出现在 {seen[c]} 与 {ch['label']}")
            seen[c] = ch["label"]
            if c not in IMPACT:
                problems.append(f"{c}: 缺传导映射 IMPACT")
    for code in FUT_CODES:
        m = reg.get(code)
        if m is None:
            problems.append(f"{code}: 适配器有映射但 registry 未登记")
        elif m.enabled and code not in seen:
            problems.append(f"{code}: 已启用但不在任何产业链分组")
    return problems
