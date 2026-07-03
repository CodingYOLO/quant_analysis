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
    # ============ 军工（材料 → 电子/元器件 → 主机厂总装） ============
    {"name": "军工", "layers": [
        {"layer": "上游·军工材料", "kind": "material", "nodes": [
            {"name": "高温合金", "leaders": [("抚顺特钢", "600399"), ("钢研高纳", "300034")]},
            {"name": "碳纤维 / 复材", "leaders": [("光威复材", "300699"), ("中简科技", "300777"), ("中航高科", "600862")]},
            {"name": "军工钛材", "leaders": [("西部超导", "688122"), ("宝钛股份", "600456")]},
        ]},
        {"layer": "中游·军工电子 / 元器件", "kind": "mfg", "nodes": [
            {"name": "连接器 / 军工电子", "leaders": [("中航光电", "002179"), ("航天电器", "002025"), ("火炬电子", "603678")]},
            {"name": "军工芯片 / 电路", "leaders": [("紫光国微", "002049"), ("振华科技", "000733"), ("宏达电子", "300726")]},
            {"name": "雷达 / 微波 / T-R芯片", "leaders": [("铖昌科技", "001270"), ("国博电子", "688375")]},
        ]},
        {"layer": "下游·主机厂 / 总装", "kind": "app", "nodes": [
            {"name": "航空整机 / 发动机", "leaders": [("中航沈飞", "600760"), ("航发动力", "600893"), ("中直股份", "600038")]},
            {"name": "航天 / 导弹", "leaders": [("航天电子", "600879"), ("航天彩虹", "002389")]},
            {"name": "军船", "leaders": [("中国船舶", "600150")]},
        ]},
    ]},
    # ============ 消费电子（零部件 → 组装/代工 → 品牌） ============
    {"name": "消费电子", "layers": [
        {"layer": "上游·核心零部件", "kind": "mfg", "nodes": [
            {"name": "光学 / 镜头模组", "leaders": [("水晶光电", "002273"), ("蓝特光学", "688127")]},
            {"name": "声学 / 精密", "leaders": [("歌尔股份", "002241")]},
            {"name": "结构件 / 金属", "leaders": [("领益智造", "002600"), ("长盈精密", "300115")]},
            {"name": "面板 / 显示", "leaders": [("京东方A", "000725"), ("TCL科技", "000100")]},
            {"name": "FPC / PCB", "leaders": [("鹏鼎控股", "002938"), ("东山精密", "002384")]},
        ]},
        {"layer": "中游·组装 / 代工", "kind": "mfg", "nodes": [
            {"name": "整机组装 / 代工", "leaders": [("立讯精密", "002475"), ("工业富联", "601138")]},
        ]},
        {"layer": "下游·品牌 / 终端", "kind": "app", "nodes": [
            {"name": "品牌 / 整机", "leaders": [("传音控股", "688036")]},
        ]},
    ]},
    # ============ 光伏（硅料/硅片 → 电池/组件 → 设备/辅材） ============
    {"name": "光伏", "layers": [
        {"layer": "上游·硅料 / 硅片", "kind": "material", "nodes": [
            {"name": "硅料", "leaders": [("通威股份", "600438"), ("大全能源", "688303")]},
            {"name": "硅片", "leaders": [("TCL中环", "002129")]},
        ]},
        {"layer": "中游·电池 / 组件 / 逆变器", "kind": "mfg", "nodes": [
            {"name": "电池片", "leaders": [("爱旭股份", "600732"), ("钧达股份", "002865")]},
            {"name": "组件", "leaders": [("隆基绿能", "601012"), ("晶澳科技", "002459"), ("天合光能", "688599"), ("晶科能源", "688223")]},
            {"name": "逆变器", "leaders": [("阳光电源", "300274"), ("锦浪科技", "300763"), ("固德威", "688390")]},
        ]},
        {"layer": "配套·设备 / 辅材", "kind": "mfg", "nodes": [
            {"name": "光伏设备", "leaders": [("迈为股份", "300751"), ("捷佳伟创", "300724")]},
            {"name": "胶膜", "leaders": [("福斯特", "603806")]},
            {"name": "光伏玻璃", "leaders": [("福莱特", "601865")]},
        ]},
    ]},
    # ============ PCB（覆铜板 → PCB制造/载板 → 设备） ============
    {"name": "PCB", "layers": [
        {"layer": "上游·覆铜板 / 材料", "kind": "material", "nodes": [
            {"name": "覆铜板(CCL)", "leaders": [("生益科技", "600183"), ("南亚新材", "688519")]},
        ]},
        {"layer": "中游·PCB制造 / 载板", "kind": "mfg", "nodes": [
            {"name": "PCB / HDI", "leaders": [("沪电股份", "002463"), ("深南电路", "002916"), ("景旺电子", "603228"), ("鹏鼎控股", "002938")]},
            {"name": "IC载板", "leaders": [("兴森科技", "002436")]},
        ]},
        {"layer": "配套·设备", "kind": "mfg", "nodes": [
            {"name": "PCB设备", "leaders": [("大族数控", "301200")]},
        ]},
    ]},
    # ============ 医疗器械（影像/设备 · IVD · 高值耗材 · 生命科学上游） ============
    {"name": "医疗器械", "layers": [
        {"layer": "影像 / 设备", "kind": "mfg", "nodes": [
            {"name": "医学影像 / 设备", "leaders": [("联影医疗", "688271"), ("迈瑞医疗", "300760"), ("开立医疗", "300633")]},
        ]},
        {"layer": "IVD / 诊断", "kind": "mfg", "nodes": [
            {"name": "体外诊断(IVD)", "leaders": [("新产业", "300832"), ("安图生物", "603658")]},
        ]},
        {"layer": "高值耗材", "kind": "app", "nodes": [
            {"name": "高值耗材(心脉/骨科)", "leaders": [("惠泰医疗", "688617"), ("心脉医疗", "688016"), ("大博医疗", "002901")]},
        ]},
        {"layer": "上游·生命科学", "kind": "material", "nodes": [
            {"name": "生命科学试剂 / 上游", "leaders": [("诺唯赞", "688105")]},
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


# ── 关键位·入局区间 叠加（对标稳智AI·每档可溯源·按交易日缓存·不拖慢地图）──────
# zone(支撑/压力/入局区间)源自日线+筹码·每日静态 → 按交易日缓存；position 用实时价现算。
_ZONE_CACHE: dict = {}                     # {yyyymmdd: {6位代码: levels_dict | None}}


def _today_key() -> str:
    import datetime
    return datetime.date.today().strftime("%Y%m%d")


def _chip_costs(provider: CompositeProvider, ts: str) -> dict | None:
    """最新筹码成本三档(cost_5/50/95pct)·取关键位所需·轻量。失败返回 None。"""
    import datetime

    import pandas as pd
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=40)).strftime("%Y%m%d")
    try:
        df = provider.get_cyq_perf(ts, start, end)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    row = df.sort_values("trade_date").iloc[-1]

    def num(key):
        v = pd.to_numeric(row.get(key), errors="coerce")
        return float(v) if pd.notna(v) else None
    return {"cost_5pct": num("cost_5pct"), "cost_50pct": num("cost_50pct"),
            "cost_95pct": num("cost_95pct")}


def _zone_for(provider: CompositeProvider, code: str) -> dict | None:
    """单只票的日线静态关键位(含 IO)·按交易日缓存。数据不足返回 None。"""
    import datetime

    from app.data.kline_loader import load_kline
    from app.strategy.key_levels import build_key_levels
    day = _today_key()
    cache = _ZONE_CACHE.setdefault(day, {})
    if code in cache:
        return cache[code]
    for d in list(_ZONE_CACHE):                    # 只留当日缓存·防泄漏
        if d != day:
            _ZONE_CACHE.pop(d, None)
    ts = _to_ts(code)
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=240)).strftime("%Y%m%d")
    lv = None
    try:
        k = load_kline(ts, start, end, provider, adj="qfq")
        if k is not None and len(k) >= 60:
            lv = build_key_levels(k, _chip_costs(provider, ts))
    except Exception:
        lv = None
    cache[code] = lv
    return lv


def _overlay_for(lv: dict | None, price: float) -> dict:
    """把日线静态 zone + 实时价 → 该票的叠加载荷(状态/入局区间/最近支撑压力)。"""
    from app.strategy.key_levels import _position
    if not lv or not lv.get("entry_zone"):
        return {"state": "na"}
    z = lv["entry_zone"]
    px = price if price > 0 else lv["price"]
    pos = _position(px, z)
    sup = (lv.get("support") or [None])[0]
    res = (lv.get("resistance") or [None])[0]
    return {"state": pos["state"], "label": pos["label"], "price": round(px, 2),
            "zlow": z["low"], "zhigh": z["high"], "basis": z["basis"],
            "sup": {"mid": sup["mid"], "srcs": sup["srcs"]} if sup else None,
            "res": {"mid": res["mid"], "srcs": res["srcs"]} if res else None,
            "as_of": lv["as_of"]}


def build_chain_levels(provider: CompositeProvider, name: str) -> dict:
    """某条链所有龙头的关键位叠加：入局区间 + 现价相对位置 + 触发汇总。

    地图渲染后异步单独拉·不拖慢主图。zone 按日缓存(静态)·position 用实时价现算。
    返回 {ok, codes:{6位:{state,label,zlow,zhigh,basis,sup,res,as_of}}, summary}。
    """
    from concurrent.futures import ThreadPoolExecutor
    chain = _CHAIN_BY_NAME.get(name) or _CHAINS[0]
    codes = sorted({code for layer in chain["layers"] for nd in layer["nodes"]
                    for _nm, code in nd["leaders"]})
    spot = _spot_map(provider)

    def one(code: str) -> tuple[str, dict | None]:
        try:
            return code, _zone_for(provider, code)
        except Exception:
            return code, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        zones = dict(ex.map(one, codes))

    out, hit_in, hit_watch = {}, [], []
    for code in codes:
        q = spot.get(code, {})
        ov = _overlay_for(zones.get(code), _num(q.get("price")))
        out[code] = ov
        nm = q.get("name") or code
        if ov["state"] == "in":
            hit_in.append(nm)
        elif ov["state"] == "watch":
            hit_watch.append(nm)
    return {"ok": True, "name": chain["name"], "codes": out,
            "summary": {"in": hit_in, "watch": hit_watch,
                        "n_in": len(hit_in), "n_watch": len(hit_watch),
                        "n_total": len(codes)}}


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
