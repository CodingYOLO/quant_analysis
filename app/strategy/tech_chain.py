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
    {"name": "半导体", "layers": [
        {"layer": "上游·设备 / 材料", "kind": "material", "nodes": [
            {"name": "半导体设备", "leaders": [("北方华创", "002371"), ("中微公司", "688012"), ("拓荆科技", "688072")]},
            {"name": "半导体材料", "leaders": [("沪硅产业", "688126"), ("鼎龙股份", "300054"), ("安集科技", "688019")]},
            {"name": "锗镓(化合物)", "leaders": [("云南锗业", "002428"), ("驰宏锌锗", "600497")]},
        ]},
        {"layer": "中游·制造 / 封测", "kind": "mfg", "nodes": [
            {"name": "晶圆制造", "leaders": [("中芯国际", "688981"), ("华虹公司", "688347")]},
            {"name": "先进封装", "leaders": [("长电科技", "600584"), ("通富微电", "002156"), ("甬矽电子", "688362")]},
            {"name": "IC设计/算力芯", "leaders": [("寒武纪", "688256"), ("海光信息", "688041")]},
        ]},
        {"layer": "下游·细分应用", "kind": "app", "nodes": [
            {"name": "存储芯片", "leaders": [("兆易创新", "603986"), ("北京君正", "300223"), ("江波龙", "301308")]},
            {"name": "模拟芯片", "leaders": [("圣邦股份", "300661"), ("纳芯微", "688052")]},
            {"name": "功率半导体", "leaders": [("斯达半导", "603290"), ("时代电气", "688187"), ("士兰微", "600460")]},
        ]},
    ]},
    {"name": "光通信·光纤·算力", "layers": [
        {"layer": "上游·光芯片 / 光纤材料", "kind": "material", "nodes": [
            {"name": "光芯片", "leaders": [("源杰科技", "688498"), ("仕佳光子", "688313"), ("光库科技", "300620")]},
            {"name": "光纤预制棒/材料", "leaders": [("长飞光纤", "688046"), ("亨通光电", "600487")]},
        ]},
        {"layer": "中游·光纤光缆 / 光模块", "kind": "mfg", "nodes": [
            {"name": "光纤光缆", "leaders": [("长飞光纤", "688046"), ("亨通光电", "600487"), ("中天科技", "600522")]},
            {"name": "光模块/CPO", "leaders": [("中际旭创", "300308"), ("新易盛", "300502"), ("天孚通信", "300394")]},
        ]},
        {"layer": "下游·算力 / 数据中心", "kind": "app", "nodes": [
            {"name": "算力/服务器", "leaders": [("浪潮信息", "000977"), ("工业富联", "601138"), ("中科曙光", "603019")]},
            {"name": "液冷", "leaders": [("英维克", "002837"), ("高澜股份", "300499")]},
        ]},
    ]},
    {"name": "稀土永磁", "layers": [
        {"layer": "上游·稀土资源", "kind": "material", "nodes": [
            {"name": "稀土资源", "leaders": [("北方稀土", "600111"), ("中国稀土", "000831"), ("广晟有色", "600259")]},
        ]},
        {"layer": "中游·磁材", "kind": "mfg", "nodes": [
            {"name": "钕铁硼磁材", "leaders": [("金力永磁", "300748"), ("中科三环", "000970"), ("正海磁材", "300224")]},
        ]},
        {"layer": "下游·应用(电机)", "kind": "app", "nodes": [
            {"name": "机器人/电机", "leaders": [("汇川技术", "300124"), ("鸣志电器", "603728")]},
            {"name": "风电/新能源车", "leaders": [("金风科技", "002202"), ("比亚迪", "002594")]},
        ]},
    ]},
    {"name": "有色·小金属", "layers": [
        {"layer": "资源·小金属", "kind": "material", "nodes": [
            {"name": "钨", "leaders": [("厦门钨业", "600549"), ("中钨高新", "000657"), ("章源钨业", "002378")]},
            {"name": "锑/锡", "leaders": [("湖南黄金", "002155"), ("华锡有色", "600301"), ("锡业股份", "000960")]},
            {"name": "锂", "leaders": [("赣锋锂业", "002460"), ("天齐锂业", "002466")]},
        ]},
        {"layer": "资源·工业金属", "kind": "material", "nodes": [
            {"name": "铜", "leaders": [("紫金矿业", "601899"), ("洛阳钼业", "603993"), ("江西铜业", "600362")]},
            {"name": "铝", "leaders": [("中国铝业", "601600"), ("云铝股份", "000807")]},
        ]},
    ]},
    {"name": "PCB·电子材料", "layers": [
        {"layer": "上游·材料", "kind": "material", "nodes": [
            {"name": "覆铜板", "leaders": [("生益科技", "600183"), ("南亚新材", "688519")]},
            {"name": "电子化学品", "leaders": [("光华科技", "002741"), ("飞凯材料", "300398")]},
        ]},
        {"layer": "中游·PCB制造", "kind": "mfg", "nodes": [
            {"name": "算力PCB", "leaders": [("沪电股份", "002463"), ("胜宏科技", "300476"), ("深南电路", "002916")]},
            {"name": "IC载板", "leaders": [("兴森科技", "002436"), ("深南电路", "002916")]},
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
