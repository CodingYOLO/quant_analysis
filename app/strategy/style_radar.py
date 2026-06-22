"""风格切换雷达：把行业宽表聚成 6 大风格，检测资金在风格间的轮动（切入/切出）。

为什么做：A 股常以"风格"成块轮动（科技成长 ↔ 周期资源 ↔ 高端制造 …）。判断
"今天是哪种风格在领涨、是不是发生了切换"，比盯单一行业更能把握主线。

数据口径：全部来自 theme_heat_all_in_one(industry) 宽表（**盘后·非实时**），含每个行业的
`money_flow_1d/5d`（资金）与 `pct_chg_1d/5d`（涨跌）。可靠、不依赖东财实时接口。

设计：纯函数 `_compute_styles` 与 DB 读取解耦，便于零网络单测；风格映射为配置化常量。
轮动判定 = 今日资金排名相对 5 日资金排名的变化（排名跃升=资金正在切入该风格）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# —— 配置：行业名 → 大风格（申万二级·131 行业·生产口径）。未覆盖者归入"综合其他"。——
# 口径以 theme_heat_all_in_one(industry) 实际行业名为准（申万二级，名带"Ⅱ"后缀者为申万二级标识）。
# 全部 131 行业均已归类（科技18/周期25/制造23/医药消费42/金融9/公用14），覆盖率 100%。
_STYLE_GROUPS: dict[str, list[str]] = {
    "科技TMT": [
        "半导体", "元件", "光学光电子", "消费电子", "其他电子Ⅱ", "电子化学品Ⅱ",
        "计算机设备", "软件开发", "IT服务Ⅱ", "通信服务", "通信设备",
        "出版", "影视院线", "数字媒体", "广告营销", "游戏Ⅱ", "电视广播Ⅱ", "互联网电商",
    ],
    "周期资源": [
        "工业金属", "贵金属", "能源金属", "小金属", "金属新材料", "冶钢原料",
        "普钢", "特钢Ⅱ", "煤炭开采", "焦炭Ⅱ", "油气开采Ⅱ", "油服工程", "炼化及贸易",
        "化学原料", "化学制品", "化学纤维", "农化制品", "塑料", "橡胶", "非金属材料Ⅱ",
        "玻璃玻纤", "水泥", "装修建材", "造纸", "包装印刷",
    ],
    "高端制造": [
        "电池", "光伏设备", "风电设备", "电网设备", "电机Ⅱ", "其他电源设备Ⅱ",
        "自动化设备", "专用设备", "通用设备", "工程机械", "轨交设备Ⅱ",
        "乘用车", "商用车", "汽车零部件", "汽车服务", "摩托车及其他",
        "航天装备Ⅱ", "航空装备Ⅱ", "航海装备Ⅱ", "军工电子Ⅱ", "地面兵装Ⅱ",
        "照明设备Ⅱ", "家电零部件Ⅱ",
    ],
    "医药消费": [
        "化学制药", "生物制品", "中药Ⅱ", "医药商业", "医疗器械", "医疗服务",
        "医疗美容", "动物保健Ⅱ",
        "白酒Ⅱ", "非白酒", "饮料乳品", "休闲食品", "食品加工", "调味发酵品Ⅱ",
        "白色家电", "黑色家电", "小家电", "厨卫电器", "其他家电Ⅱ",
        "个护用品", "化妆品", "饰品", "服装家纺", "纺织制造", "家居用品",
        "一般零售", "专业连锁Ⅱ", "贸易Ⅱ", "旅游及景区", "旅游零售Ⅱ", "酒店餐饮",
        "教育", "体育Ⅱ", "文娱用品", "专业服务",
        "养殖业", "农产品加工", "种植业", "渔业", "饲料", "林业Ⅱ", "农业综合Ⅱ",
    ],
    "金融地产": [
        "国有大型银行Ⅱ", "股份制银行Ⅱ", "城商行Ⅱ", "农商行Ⅱ",
        "证券Ⅱ", "保险Ⅱ", "多元金融", "房地产开发", "房地产服务",
    ],
    "公用基建": [
        "电力", "燃气Ⅱ", "环保设备Ⅱ", "环境治理",
        "基础建设", "专业工程", "工程咨询服务Ⅱ", "装修装饰Ⅱ", "房屋建设Ⅱ",
        "物流", "铁路公路", "航运港口", "航空机场", "综合Ⅱ",
    ],
}
_STYLE_ORDER: list[str] = list(_STYLE_GROUPS.keys()) + ["综合其他"]

# 行业名 → 风格 的反查表（构建一次）。
_STYLE_OF: dict[str, str] = {
    name: style for style, names in _STYLE_GROUPS.items() for name in names
}

_ROTATION_RANK_GAP = 2      # 今日排名相对5日排名跃升/下滑达此名次 → 判为切入/切出
_DEFAULT_STYLE = "综合其他"


@dataclass
class StyleMomentum:
    """单个风格的资金动量与轮动结果（单位：资金=亿、涨跌=%）。"""

    style: str
    n: int                       # 有数据的成员行业数
    flow_1d_yi: float            # 今日资金（Σ成员 money_flow_1d）
    flow_5d_yi: float            # 近5日资金（Σ成员 money_flow_5d）
    pct_1d: float                # 今日涨跌（成员均值）
    pct_5d: float                # 近5日涨跌（成员均值）
    heat: float                  # 风格热度（成员 heat_score 均值）
    rank_1d: int                 # 今日资金排名（1=最强）
    rank_5d: int                 # 近5日资金排名
    rotation: str                # 切入↑ / 切出↓ / 持平
    top_industries: list[dict]   # 风格内今日资金前3行业 [{name, flow_1d, pct_1d}]

    def to_dict(self) -> dict:
        return asdict(self)


def style_of(industry_name: str) -> str:
    """行业名 → 大风格；未覆盖归入"综合其他"。"""
    return _STYLE_OF.get((industry_name or "").strip(), _DEFAULT_STYLE)


def _rotation(rank_1d: int, rank_5d: int, gap: int = _ROTATION_RANK_GAP) -> str:
    """由"今日资金排名"相对"5日资金排名"的变化判轮动方向（纯函数·可单测）。

    排名 1=最强。今日排名比5日明显靠前(数值更小) → 资金正切入该风格(切入↑)。
    """
    diff = rank_5d - rank_1d           # >0 表示今日比5日更靠前（跃升）
    if diff >= gap:
        return "切入↑"
    if diff <= -gap:
        return "切出↓"
    return "持平"


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _compute_styles(industry_rows: list[dict]) -> list[StyleMomentum]:
    """把行业宽表行聚成风格动量列表（纯函数·零依赖·可单测）。

    Args:
        industry_rows: 行业宽表记录，每条含 theme_name/money_flow_1d/money_flow_5d/
            pct_chg_1d/pct_chg_5d/heat_score。

    Returns:
        按今日资金降序的 StyleMomentum 列表（含排名与轮动判定）。
    """
    buckets: dict[str, dict] = {}
    for r in industry_rows:
        style = style_of(r.get("theme_name"))
        b = buckets.setdefault(style, {"members": [], "flow_1d": 0.0, "flow_5d": 0.0,
                                       "pct_1d": [], "pct_5d": [], "heat": []})
        f1 = _num(r.get("money_flow_1d"))
        b["flow_1d"] += f1
        b["flow_5d"] += _num(r.get("money_flow_5d"))
        b["pct_1d"].append(_num(r.get("pct_chg_1d")))
        b["pct_5d"].append(_num(r.get("pct_chg_5d")))
        if r.get("heat_score") is not None:
            b["heat"].append(_num(r.get("heat_score")))
        b["members"].append({"name": r.get("theme_name", ""), "flow_1d": round(f1, 2),
                             "pct_1d": round(_num(r.get("pct_chg_1d")), 2)})

    styles = [_finalize(style, b) for style, b in buckets.items() if b["members"]]
    # 排名：今日资金 / 5日资金（1=最强）
    rank_1d = {s.style: i + 1 for i, s in enumerate(sorted(styles, key=lambda x: -x.flow_1d_yi))}
    rank_5d = {s.style: i + 1 for i, s in enumerate(sorted(styles, key=lambda x: -x.flow_5d_yi))}
    for s in styles:
        s.rank_1d = rank_1d[s.style]
        s.rank_5d = rank_5d[s.style]
        s.rotation = _rotation(s.rank_1d, s.rank_5d)
    styles.sort(key=lambda x: x.rank_1d)
    return styles


def _finalize(style: str, b: dict) -> StyleMomentum:
    """把单风格的累加桶折叠成 StyleMomentum（排名稍后回填）。"""
    n = len(b["members"])
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else 0.0
    top = sorted(b["members"], key=lambda m: -m["flow_1d"])[:3]
    return StyleMomentum(
        style=style, n=n,
        flow_1d_yi=round(b["flow_1d"], 2), flow_5d_yi=round(b["flow_5d"], 2),
        pct_1d=avg(b["pct_1d"]), pct_5d=avg(b["pct_5d"]), heat=avg(b["heat"]),
        rank_1d=0, rank_5d=0, rotation="持平", top_industries=top,
    )


def build_style_radar(date: str | None = None) -> dict:
    """读行业宽表(盘后) → 6 大风格资金动量 + 轮动检测。

    Returns:
        {ok, date, styles:[StyleMomentum...], rotating_in:[风格名], rotating_out:[风格名]}
        无数据时 {ok:False, msg}。
    """
    from app.data.theme_heat_db import get_themes, latest_trade_date
    d = (date or "").replace("-", "") or latest_trade_date("industry")
    if not d:
        return {"ok": False, "msg": "暂无行业宽表数据"}
    rows = get_themes(d, "industry")
    if not rows:
        return {"ok": False, "msg": f"{d} 无行业宽表数据"}
    styles = _compute_styles(rows)
    return {
        "ok": True, "date": d,
        "styles": [s.to_dict() for s in styles],
        "rotating_in": [s.style for s in styles if s.rotation == "切入↑"],
        "rotating_out": [s.style for s in styles if s.rotation == "切出↓"],
    }
