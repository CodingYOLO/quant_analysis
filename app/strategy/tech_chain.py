"""科技+材料产业链地图：资源材料 → 制造 → 应用 三层，每环挂真·龙头中军，
按龙头的实时表现给节点上色、并把"领头羊"（今日领涨的龙头）顶出来。

设计要点：
- 节点强弱**不靠概念名匹配**（易错），直接用该环【龙头篮子】的实时涨跌幅聚合 → 更准、更直接。
- 领头羊 = 龙头篮子里今日涨幅最高者；同时标出结构龙头（配置里的第一个）。
- 今日风格 = 全链「资源材料层」vs「制造+应用层」平均强度对比 → 看高低切/风格切换。
- 龙头为手工梳理（行业地位排序），可在 _CHAINS 维护扩展。
"""

from __future__ import annotations

import time

from app.data.composite_provider import CompositeProvider

# 链条配置：每个节点 = (环节名, [(龙头名, 6位代码) ...]，结构龙头排在最前)
# layer.kind: material(资源材料) / mfg(制造) / app(应用)
_CHAINS: list[dict] = [
    # ============ AI 算力·芯片（设计-制造-封装-存储） ============
    {"name": "AI算力·芯片", "layers": [
        {"layer": "上游·半导体设备（前道·国产替代）", "kind": "material", "nodes": [
            {"name": "刻蚀设备", "leaders": [("中微公司", "688012"), ("北方华创", "002371")]},
            {"name": "薄膜沉积(CVD/ALD)", "leaders": [("拓荆科技", "688072"), ("北方华创", "002371")]},
            {"name": "涂胶显影机", "leaders": [("芯源微", "688037")]},
            {"name": "清洗设备", "leaders": [("盛美上海", "688082"), ("至纯科技", "603690"), ("北方华创", "002371")]},
            {"name": "CMP抛光设备", "leaders": [("华海清科", "688120")]},
            {"name": "量测 / 检测设备", "leaders": [("中科飞测", "688361"), ("精测电子", "300567")]},
            {"name": "长晶炉 / 热场", "leaders": [("晶盛机电", "300316"), ("金博股份", "688598")]},
        ]},
        {"layer": "上游·半导体材料 / EDA", "kind": "material", "nodes": [
            {"name": "硅片", "leaders": [("沪硅产业", "688126"), ("立昂微", "605358"), ("TCL中环", "002129")]},
            {"name": "电子特种气体", "leaders": [("华特气体", "688268"), ("金宏气体", "688106"), ("南大光电", "300346")]},
            {"name": "光刻胶", "leaders": [("彤程新材", "603650"), ("南大光电", "300346"), ("晶瑞电材", "300655")]},
            {"name": "CMP抛光材料", "leaders": [("安集科技", "688019"), ("鼎龙股份", "300054")]},
            {"name": "湿电子化学品", "leaders": [("江化微", "603078"), ("晶瑞电材", "300655"), ("格林达", "603931")]},
            {"name": "靶材", "leaders": [("江丰电子", "300666"), ("有研新材", "600206"), ("隆华科技", "300263")]},
            {"name": "光掩模", "leaders": [("清溢光电", "688138"), ("路维光电", "688401")]},
            {"name": "封装材料", "leaders": [("华海诚科", "688535"), ("联瑞新材", "688300")]},
            {"name": "EDA / IP", "leaders": [("华大九天", "301269"), ("概伦电子", "688206"), ("广立微", "301095")]},
        ]},
        {"layer": "中游·设计 / 制造 / 封测", "kind": "mfg", "nodes": [
            {"name": "AI芯片/GPU", "leaders": [("寒武纪", "688256"), ("海光信息", "688041"), ("景嘉微", "300474")]},
            {"name": "ASIC / SoC / 接口芯片", "leaders": [("澜起科技", "688008"), ("瑞芯微", "603893"), ("全志科技", "300458"), ("乐鑫科技", "688018")]},
            {"name": "晶圆代工", "leaders": [("中芯国际", "688981"), ("华虹宏力", "688347")]},
            {"name": "先进封装 / 3D堆叠", "leaders": [("通富微电", "002156"), ("长电科技", "600584"), ("晶方科技", "603005"), ("甬矽电子", "688362"), ("华天科技", "002185"), ("深科技", "000021")]},
            {"name": "测试 / 测试机", "leaders": [("华峰测控", "688200"), ("长川科技", "300604"), ("利扬芯片", "688135")]},
        ]},
        {"layer": "下游·存储 / 模拟 / 功率 / 元件", "kind": "app", "nodes": [
            {"name": "存储 / HBM", "leaders": [("兆易创新", "603986"), ("香农芯创", "300475"), ("江波龙", "301308"), ("德明利", "001309"), ("佰维存储", "688525")]},
            {"name": "模拟 / 射频 / CIS", "leaders": [("豪威集团", "603501"), ("圣邦股份", "300661"), ("卓胜微", "300782"), ("思瑞浦", "688536"), ("纳芯微", "688052")]},
            {"name": "功率 / 第三代半导体", "leaders": [("斯达半导", "603290"), ("时代电气", "688187"), ("士兰微", "600460"), ("三安光电", "600703"), ("华润微", "688396")]},
            {"name": "被动元件 / MLCC", "leaders": [("三环集团", "300408"), ("风华高科", "000636"), ("顺络电子", "002138"), ("麦捷科技", "300319")]},
        ]},
    ]},
    # ============ AI 算力·光模块 / PCB / 连接 ============
    {"name": "AI算力·光/PCB/连接", "layers": [
        {"layer": "上游·光芯片 / 覆铜板", "kind": "material", "nodes": [
            {"name": "光芯片", "leaders": [("源杰科技", "688498"), ("仕佳光子", "688313"), ("长光华芯", "688048"), ("光库科技", "300620")]},
            {"name": "覆铜板(CCL)", "leaders": [("生益科技", "600183"), ("南亚新材", "688519"), ("华正新材", "603186")]},
            {"name": "光纤预制棒", "leaders": [("长飞光纤", "601869"), ("亨通光电", "600487"), ("中天科技", "600522")]},
        ]},
        {"layer": "中游·光模块 / 算力PCB", "kind": "mfg", "nodes": [
            {"name": "光模块 / CPO", "leaders": [("中际旭创", "300308"), ("新易盛", "300502"), ("天孚通信", "300394"), ("光迅科技", "002281"), ("太辰光", "300570")]},
            {"name": "算力PCB / HDI", "leaders": [("沪电股份", "002463"), ("胜宏科技", "300476"), ("深南电路", "002916"), ("兴森科技", "002436")]},
        ]},
        {"layer": "下游·连接 / 交换", "kind": "app", "nodes": [
            {"name": "高速连接器", "leaders": [("华丰科技", "688629"), ("鼎通科技", "688668"), ("意华股份", "002897"), ("中航光电", "002179")]},
            {"name": "铜缆 / 线材", "leaders": [("沃尔核材", "002130"), ("精达股份", "600577"), ("神宇股份", "300563"), ("金信诺", "300252")]},
            {"name": "交换 / 网络芯片", "leaders": [("盛科通信", "688702"), ("裕太微", "688515")]},
            {"name": "交换机 / 网络设备", "leaders": [("紫光股份", "000938"), ("锐捷网络", "301165")]},
        ]},
    ]},
    # ============ AI 算力·服务器 / 电源 / 散热 / IDC ============
    {"name": "AI算力·服务器/散热", "layers": [
        {"layer": "上游·电源 / 散热", "kind": "material", "nodes": [
            {"name": "服务器电源", "leaders": [("麦格米特", "002851"), ("欧陆通", "300870"), ("新雷能", "300593")]},
            {"name": "液冷 / 温控", "leaders": [("英维克", "002837"), ("申菱环境", "301018"), ("高澜股份", "300499"), ("同飞股份", "300990")]},
            {"name": "散热 / 均热VC", "leaders": [("中石科技", "300684"), ("飞荣达", "300602")]},
        ]},
        {"layer": "中游·AI服务器 / 整机", "kind": "mfg", "nodes": [
            {"name": "AI服务器", "leaders": [("工业富联", "601138"), ("浪潮信息", "000977"), ("中科曙光", "603019"), ("拓维信息", "002261")]},
        ]},
        {"layer": "下游·数据中心 IDC", "kind": "app", "nodes": [
            {"name": "IDC / 算力租赁", "leaders": [("润泽科技", "300442"), ("光环新网", "300383"), ("数据港", "603881"), ("奥飞数据", "300738")]},
        ]},
    ]},
    # ============ AI 应用·大模型 / 软件 / 端侧 ============
    {"name": "AI应用·大模型/软件", "layers": [
        {"layer": "上游·大模型 / 算法", "kind": "mfg", "nodes": [
            {"name": "大模型 / AGI", "leaders": [("科大讯飞", "002230"), ("三六零", "601360"), ("昆仑万维", "300418")]},
            {"name": "多模态 / 内容生成", "leaders": [("万兴科技", "300624")]},
        ]},
        {"layer": "中游·AI应用软件", "kind": "mfg", "nodes": [
            {"name": "AI办公 / 协同", "leaders": [("金山办公", "688111"), ("福昕软件", "688095"), ("泛微网络", "603039")]},
            {"name": "AI+金融", "leaders": [("同花顺", "300033"), ("恒生电子", "600570")]},
            {"name": "AI+医疗 / 政务", "leaders": [("卫宁健康", "300253"), ("拓尔思", "300229")]},
        ]},
        {"layer": "下游·端侧 AI", "kind": "app", "nodes": [
            {"name": "端侧AI / OS", "leaders": [("中科创达", "300496"), ("传音控股", "688036")]},
        ]},
    ]},
    # ============ 具身智能·机器人（AI 硬件下游） ============
    {"name": "具身智能·机器人", "layers": [
        {"layer": "上游·核心零部件", "kind": "mfg", "nodes": [
            {"name": "减速器", "leaders": [("绿的谐波", "688017"), ("双环传动", "002472")]},
            {"name": "丝杠 / 执行器", "leaders": [("拓普集团", "601689"), ("三花智控", "002050"), ("五洲新春", "603667"), ("北特科技", "603009")]},
            {"name": "无框电机", "leaders": [("鸣志电器", "603728"), ("步科股份", "688160")]},
        ]},
        {"layer": "中游·感知 / 灵巧手", "kind": "mfg", "nodes": [
            {"name": "力 / 触觉传感器", "leaders": [("柯力传感", "603662"), ("汉威科技", "300007")]},
            {"name": "灵巧手 / 微传动", "leaders": [("兆威机电", "003021")]},
        ]},
        {"layer": "下游·运控 / 本体", "kind": "app", "nodes": [
            {"name": "运动控制 / 本体", "leaders": [("汇川技术", "300124"), ("埃斯顿", "002747")]},
        ]},
    ]},
    # ============ 稀土永磁（电机 / 机器人材料·高低切观察） ============
    {"name": "稀土永磁", "layers": [
        {"layer": "上游·稀土资源", "kind": "material", "nodes": [
            {"name": "稀土资源", "leaders": [("北方稀土", "600111"), ("中国稀土", "000831"), ("中稀有色", "600259")]},
        ]},
        {"layer": "中游·磁材", "kind": "mfg", "nodes": [
            {"name": "钕铁硼磁材", "leaders": [("金力永磁", "300748"), ("中科三环", "000970"), ("正海磁材", "300224")]},
        ]},
        {"layer": "下游·应用(电机)", "kind": "app", "nodes": [
            {"name": "机器人 / 电机", "leaders": [("汇川技术", "300124"), ("鸣志电器", "603728")]},
            {"name": "风电 / 新能源车", "leaders": [("金风科技", "002202"), ("比亚迪", "002594")]},
        ]},
    ]},
    # ============ 有色·小金属（资源端·高低切观察） ============
    {"name": "有色·小金属", "layers": [
        {"layer": "资源·小金属", "kind": "material", "nodes": [
            {"name": "钨", "leaders": [("厦门钨业", "600549"), ("中钨高新", "000657"), ("章源钨业", "002378")]},
            {"name": "锑 / 锡", "leaders": [("湖南黄金", "002155"), ("华锡有色", "600301"), ("锡业股份", "000960")]},
            {"name": "锗(半导体/红外)", "leaders": [("云南锗业", "002428"), ("驰宏锌锗", "600497")]},
            {"name": "黄金(避险/高低切)", "leaders": [("山东黄金", "600547"), ("赤峰黄金", "600988")]},
            {"name": "锂", "leaders": [("赣锋锂业", "002460"), ("天齐锂业", "002466")]},
        ]},
        {"layer": "资源·工业金属", "kind": "material", "nodes": [
            {"name": "铜", "leaders": [("紫金矿业", "601899"), ("洛阳钼业", "603993"), ("江西铜业", "600362")]},
            {"name": "铝", "leaders": [("中国铝业", "601600"), ("云铝股份", "000807")]},
        ]},
    ]},
]

_CHAIN_BY_NAME = {c["name"]: c for c in _CHAINS}
_HOT, _WARM, _COLD = 3.0, 0.5, -1.5      # 节点平均涨幅 → 强弱分档(%)
_SPOT_CACHE: dict = {}                    # {ts: spotmap}（60秒缓存·避免重复拉报价）
_STALE: list = []                         # 最近一次成功的 spotmap（拉取失败时兜底，避免空屏）


def chain_names() -> list[str]:
    return [c["name"] for c in _CHAINS]


def _to_ts(code: str) -> str:
    """6位代码 → Tushare 格式(.SH/.SZ/.BJ)，供新浪批量报价。"""
    c = str(code).zfill(6)
    if c[0] in ("6", "9") or c[:3] in ("688", "689"):
        return c + ".SH"
    if c[0] in ("8", "4") or c[:3] == "920":
        return c + ".BJ"
    return c + ".SZ"


def _all_codes() -> list[str]:
    codes: set[str] = set()
    for c in _CHAINS:
        for layer in c["layers"]:
            for nd in layer["nodes"]:
                for _nm, code in nd["leaders"]:
                    codes.add(code)
    return sorted(codes)


def _spot_map(provider: CompositeProvider) -> dict:
    """龙头实时报价（新浪·只查配置内龙头·国内直连稳）→ {6位代码: {pct, price, name}}。

    60秒缓存；拉取失败时返回上次成功结果（避免东财全市场扫描的限流问题）。
    """
    now = time.time()
    for ts in list(_SPOT_CACHE):
        if now - ts > 60:
            _SPOT_CACHE.pop(ts, None)
    if _SPOT_CACHE:
        return next(iter(_SPOT_CACHE.values()))
    out: dict = {}
    try:
        df = provider.get_realtime_quote([_to_ts(c) for c in _all_codes()])
        if df is not None and not df.empty:
            for r in df.to_dict("records"):
                code = str(r.get("ts_code", "")).split(".")[0]
                out[code] = {"pct": _num(r.get("pct_chg")), "price": r.get("price"),
                             "name": str(r.get("name", ""))}
    except Exception:
        out = {}
    if out:
        _SPOT_CACHE[now] = out
        _STALE[:] = [out]
        return out
    return _STALE[0] if _STALE else {}


def _num(v) -> float:
    try:
        import pandas as pd
        x = pd.to_numeric(v, errors="coerce")
        return float(x) if x == x else 0.0
    except Exception:
        return 0.0


def _node_leaders(node: dict, spot: dict) -> tuple[list[dict], float]:
    """节点龙头篮子 → 带实时涨幅的列表(领涨置顶) + 节点平均强度。"""
    leaders = []
    for i, (nm, code) in enumerate(node["leaders"]):
        q = spot.get(code, {})
        leaders.append({"name": nm, "code": code, "pct": q.get("pct"),
                        "price": q.get("price"), "is_anchor": i == 0})   # 结构龙头=配置第一个
    rated = [x for x in leaders if x["pct"] is not None]
    rated.sort(key=lambda x: x["pct"], reverse=True)                     # 今日领涨置顶
    others = [x for x in leaders if x["pct"] is None]
    avg = round(sum(x["pct"] for x in rated) / len(rated), 2) if rated else None
    return rated + others, (avg if avg is not None else 0.0)


def _level(avg: float) -> str:
    if avg >= _HOT:
        return "strong"
    if avg >= _WARM:
        return "warm"
    if avg <= _COLD:
        return "weak"
    return "neutral"


def build_chain(provider: CompositeProvider, name: str) -> dict:
    """构建某条链的产业链地图（含各环龙头实时表现 + 上色 + 今日风格 + 数据时间）。"""
    import datetime
    chain = _CHAIN_BY_NAME.get(name) or _CHAINS[0]
    spot = _spot_map(provider)
    as_of = ""
    if _SPOT_CACHE:
        as_of = datetime.datetime.fromtimestamp(max(_SPOT_CACHE.keys())).strftime("%m-%d %H:%M:%S")
    layers = []
    for layer in chain["layers"]:
        nodes = []
        for nd in layer["nodes"]:
            leaders, avg = _node_leaders(nd, spot)
            nodes.append({"name": nd["name"], "avg_pct": avg, "level": _level(avg),
                          "anchor": next((x["name"] for x in leaders if x.get("is_anchor")), ""),
                          "lead": leaders[0] if leaders else None, "leaders": leaders})
        layers.append({"layer": layer["layer"], "kind": layer["kind"], "nodes": nodes})
    return {"ok": True, "name": chain["name"], "layers": layers, "style": today_style(provider),
            "as_of": as_of, "source": "新浪实时报价·60秒缓存"}


def today_style(provider: CompositeProvider) -> dict:
    """全链资源材料层 vs 制造+应用层 平均强度 → 风格判断（高低切/成长 vs 资源）。"""
    spot = _spot_map(provider)
    mat, tech = [], []
    for c in _CHAINS:
        for layer in c["layers"]:
            for nd in layer["nodes"]:
                _, avg = _node_leaders(nd, spot)
                (mat if layer["kind"] == "material" else tech).append(avg)
    m = round(sum(mat) / len(mat), 2) if mat else 0.0
    t = round(sum(tech) / len(tech), 2) if tech else 0.0
    if m - t >= 1.0:
        txt = f"资金偏向【资源/材料端】(材料层均{m:+.1f}% > 科技制造应用{t:+.1f}%)——高低切信号，注意成长高位降温"
        lv = "material"
    elif t - m >= 1.0:
        txt = f"资金偏向【科技成长端】(制造应用均{t:+.1f}% > 资源材料{m:+.1f}%)——成长风格占优"
        lv = "tech"
    else:
        txt = f"资源材料({m:+.1f}%)与科技成长({t:+.1f}%)强度接近——风格未明显切换"
        lv = "balanced"
    return {"material_avg": m, "tech_avg": t, "text": txt, "lean": lv}
