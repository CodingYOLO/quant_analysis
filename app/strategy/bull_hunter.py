"""
牛股发掘（Bull Hunter）：左侧·埋伏引擎。

与选股池/scout/广度雷达（右侧·强势，票已经涨）互补，本模块专做**左侧潜伏**：
    政策/新闻热点催化 → 映射同花顺概念板块 → 板块内选「还没涨太快 + 真业绩 + 资金刚流入」的潜伏票。

两层：
  - Layer 1 `discover_catalysts`：从真实新闻/打板题材/概念热度趋势抽取催化，LLM **只能映射到我们库里真实存在
    的概念**（受限词表），杜绝死链与臆造政策。
  - Layer 2 `find_ambush_stocks`：对某概念成分先批量预筛位置/资金/量价，再对少量候选逐只取真业绩+事件面，
    按埋伏分排名。

诚实红线（符合 CLAUDE.md 禁止项）：
  - 埋伏=赌未来、胜率天然低 → 显式提示轻仓·分散·催化证伪即走；
  - **三重硬门槛**（真业绩 + 资金初入 + 催化可核）硬过滤价值陷阱与纯题材；
  - 缺数据显式标注、禁 mock、不输出「必涨/胜率」、不预测涨跌、不给买卖指令。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from pathlib import Path

from app.config import get_settings
from app.data.composite_provider import CompositeProvider

logger = logging.getLogger(__name__)

_DISCLAIMER = (
    "牛股发掘=左侧埋伏（赌催化兑现），胜率天然低于右侧追强；"
    "仅供研究、非涨跌预测、不构成投资建议。请轻仓·分散·催化证伪即走。"
)

# ── 催化层常量（可调）────────────────────────────────────────────────────────
# 博查检索词：聚焦政策/产业风口（真实新闻，反编造由 LLM prompt 红线把关）
_CATALYST_QUERIES = [
    "A股 十五五规划 重点扶持 产业 政策",
    "A股 国家政策 利好 板块 龙头 新质生产力",
    "A股 产业政策 大基金 扶持 半导体 人工智能 低空经济",
]
_MAX_CATALYSTS = 8           # 输出催化条数上限
_CONCEPT_VOCAB_TOPN = 200    # 喂给 LLM 的允许概念词表上限（按热度优先，控 prompt 长度）
_CATALYST_MAX_TOKENS = 6000  # 催化抽取的 LLM 额度：v4-pro 推理+输出共用此额度，需留足（见 _llm_extract_catalysts）
_RISING_DELTA_MIN = 0.0      # 概念热度 3 日变化 > 此值视为「上升/风口未退潮」

# 🎯 科技赛道聚焦（用户主投：半导体/存储/液冷/CPO/光/算力/PCB/AI…）。tech_only 时启用。
_TECH_KEYWORDS = (
    "半导体", "芯", "存储", "封装", "CPO", "光", "算力", "液冷", "PCB", "硅", "晶圆",
    "HBM", "光刻", "EDA", "服务器", "数据中心", "AI", "面板", "元件", "消费电子",
    "通信", "射频", "功率", "第三代", "MCU", "传感", "电路", "代工", "存储器", "GPU", "算法",
)
_CATALYST_TECH_QUERIES = [
    "A股 半导体 存储芯片 先进封装 国产替代 政策 大基金 扩产 订单",
    "A股 CPO 光模块 光通信 算力 液冷 数据中心 AI 产业链 景气",
    "A股 PCB 第三代半导体 光刻 EDA 设备材料 国产化 催化 政策",
]
_RESEARCH_TECH_QUERIES = [
    "A股 半导体 存储 CPO 光模块 算力 券商 研报 评级上调 金股",
    "A股 液冷 PCB 先进封装 第三代半导体 行业 深度研报 景气度 看好",
    "A股 科技 芯片 AI算力 首席分析师 研报 强烈推荐 买入",
]


def _is_tech_concept(name: str) -> bool:
    """概念名是否属于科技赛道（命中关键词即是）。纯函数，便于单测。非字符串(NaN/None)→False。"""
    s = name if isinstance(name, str) else ""           # 防 NaN(float) 让 `k in s` 崩溃
    return any(k in s for k in _TECH_KEYWORDS)

# ── 埋伏层常量（可调）────────────────────────────────────────────────────────
_AMBUSH_MIN_CAP = 100.0      # 市值下限(亿)：较选股池(200)放宽，纳入更多左侧中盘埋伏标的
_AMBUSH_MAX_CAP = 3000.0     # 市值上限(亿)：大象难埋伏
_AMBUSH_PRESCREEN_TOPN = 12  # 批量预筛后逐只取财务的候选数（限频友好·有界）
_AMBUSH_OUTPUT_TOPN = 12     # 最终输出候选数

# 埋伏分各维度满分（合计 100）
_W_PERF, _W_CATA, _W_FLOW, _W_POS, _W_VOL = 30.0, 25.0, 20.0, 15.0, 10.0

# 真业绩门槛/坡度
_PERF_NP_YOY_FULL = 50.0     # 净利同比 0→50% 给满第一档分
_PERF_ROE_LO, _PERF_ROE_HI = 5.0, 20.0
# 资金初入：主力近3日净流入(亿) 0→此值 给满分（再大也封顶，避免「已爆量」反而高分）
_FLOW_FULL_YI = 2.0
# 位置（越没涨越加分，与选股池风险项相反）
_POS_BIAS_HI = 25.0          # 20日乖离率 -5%→25% 由满分线性衰减到 0
_POS_BIAS_LO = -5.0
_POS_DIST_FAR = 30.0         # 距120日高 0→-30% 由 0 线性升到满分（离高点越远越有空间）
_POS_CHG_HI = 20.0           # 7日涨幅 0→20% 由满分线性衰减到 0
# 量能温和放大（帐篷函数：太缩=没启动、太爆=已过热）
_VOL_MILD_LO, _VOL_MILD_HI, _VOL_BLOWOFF = 1.0, 2.0, 3.5
# 避雷扣分上限
_PENALTY_MAX = 18.0
_FLOAT_NEAR_DAYS = 30        # 解禁临近天数阈值
_FLOAT_BIG_RATIO = 3.0       # 解禁比例(%)大于此视为显著抛压
_BLOCK_DISCOUNT = 2.0        # 大宗折价(%)大于此视为出货


# ──────────────────────────────────────────────────────────────────────────
# 通用小工具
# ──────────────────────────────────────────────────────────────────────────

def _ramp(v: float, lo: float, hi: float) -> float:
    """线性映射到 [0,1]：v≤lo→0，v≥hi→1，中间线性。lo==hi 时退化为阶跃。"""
    if hi <= lo:
        return 1.0 if v >= hi else 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _parse_json_array(raw: str) -> list | None:
    """从 LLM 输出稳健提取 JSON 数组（复用 theme_llm 的去代码块思路）。"""
    if not raw:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", s, flags=re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, list) else None
    except Exception:
        return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────────────────
# JSON 缓存（按日期键，可随时重算，零 DB 迁移）
# ──────────────────────────────────────────────────────────────────────────

def _cache_path(kind: str, key: str) -> Path:
    d = get_settings().cache_dir / kind
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w一-龥.-]+", "_", key)
    return d / f"{safe}.json"


def _cache_get(kind: str, key: str) -> dict | None:
    p = _cache_path(kind, key)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data["cached"] = True
        return data
    except Exception:
        return None


def _cache_put(kind: str, key: str, data: dict) -> None:
    try:
        payload = {k: v for k, v in data.items() if k != "cached"}
        _cache_path(kind, key).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("[牛股发掘] 缓存写入失败 %s/%s: %s", kind, key, e)


# ══════════════════════════════════════════════════════════════════════════
# Layer 1 · 政策/热点催化层
# ══════════════════════════════════════════════════════════════════════════

def _resolve_data_date(d: str) -> str:
    """把请求日解析为「实际有完整盘后数据的交易日」。

    当日盘后数据未入库（如盘中打开当天）→ 回退到宽表里最近已落库的交易日。
    新闻是实时联网检索的，不受此影响；仅板块热度/埋伏所需的宽表/资金按此回退。
    """
    try:
        from app.data.theme_heat_db import latest_trade_date
        latest = latest_trade_date("concept")
        if latest and d and d > latest:
            return latest
    except Exception:
        pass
    return d


def discover_catalysts(date: str, provider: CompositeProvider | None = None,
                       client=None, force: bool = False, tech_only: bool = False) -> dict:
    """
    抽取当日政策/新闻/题材催化，并映射到我们库里真实存在的同花顺概念。

    Args:
        date:      交易日 YYYYMMDD。
        provider:  数据源（依赖注入，便于单测）。
        client:    LLMClient（可注入，零网络单测）。
        force:     True 则忽略缓存重算。
        tech_only: 🎯 只看科技赛道（半导体/存储/液冷/CPO/光/算力…）：用科技检索词 + 词表过滤到科技板块。
    Returns:
        {ok, date, tech_only, catalysts:[{catalyst,type,related_concepts:[{name,heat,rising}],evidence,rising}],
         disclaimer, cached, generated_at, msg}
    """
    d = (date or "").replace("-", "")
    eff = _resolve_data_date(d)             # 数据日（盘后未入库则回退）；d 仅作展示请求日
    ck = eff + ("_tech" if tech_only else "")
    if not force:
        hit = _cache_get("bull_catalyst", ck)
        if hit:
            hit["date"] = d                # 展示用请求日（如当天），数据仍来自 eff
            hit["data_date"] = eff
            return hit

    provider = provider or CompositeProvider()
    vocab, heat_map = _concept_vocab(provider, eff)
    if tech_only:
        vocab = [n for n in vocab if _is_tech_concept(n)]   # 词表收窄到科技板块 → LLM 只映射科技
    if not vocab:
        return {"ok": False, "date": d, "data_date": eff, "tech_only": tech_only, "catalysts": [],
                "msg": "概念成分映射为空（宽表/概念缓存未就绪）"}

    news = _gather_catalyst_news(provider, eff, tech_only)
    if not news:
        logger.info("[牛股发掘] %s 无可用新闻源（博查未配置或无结果），催化层降级为空", eff)

    raw = _llm_extract_catalysts(news, vocab, heat_map, eff, client)
    catalysts = _normalize_catalysts(raw, set(vocab), heat_map, news)

    result = {
        "ok": bool(catalysts), "date": d, "data_date": eff, "tech_only": tech_only, "catalysts": catalysts,
        "disclaimer": _DISCLAIMER, "cached": False,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "msg": "" if catalysts else (
            "科技赛道暂无可映射的催化（可切回全部或刷新）" if tech_only
            else "未能从真实新闻中抽取到可映射的催化（可能新闻源为空）"),
    }
    if catalysts:                 # 仅缓存有效结果，避免把空结果固化
        _cache_put("bull_catalyst", ck, result)
    return result


def _concept_vocab(provider: CompositeProvider, date: str) -> tuple[list[str], dict]:
    """
    允许词表 = 我们真实拥有成分的同花顺概念名（按当日热度优先排序，控 prompt 长度）。
    返回 (vocab_names, heat_map{name:{heat,rising}})。
    """
    from app.factors.theme_wide import concept_members_map
    try:
        members = concept_members_map(provider)
    except Exception:
        logger.exception("[牛股发掘] 概念成分映射获取失败")
        members = {}
    if not members:
        return [], {}

    heat_map: dict[str, dict] = {}
    try:
        from app.data.theme_heat_db import get_themes, latest_trade_date
        rows = get_themes(date, "concept")
        if not rows:                       # 该日无宽表(如非宽表交易日)→退到最近有热度的交易日
            ld = latest_trade_date("concept")
            rows = get_themes(ld, "concept") if ld else []
        for r in rows:
            name = r.get("theme_name")
            if name in members:
                delta = _num(r.get("heat_score_delta_3d")) or 0.0
                heat_map[name] = {"heat": round(_num(r.get("heat_score")) or 0.0, 1),
                                  "rising": delta > _RISING_DELTA_MIN, "delta": round(delta, 1)}
    except Exception:
        logger.debug("[牛股发掘] 概念热度读取失败，词表退化为无序")

    # 有热度的概念优先（风口靠前），其余补齐
    hot = [n for n, _ in sorted(heat_map.items(), key=lambda kv: kv[1]["heat"], reverse=True)]
    rest = [n for n in members if n not in heat_map]
    vocab = (hot + rest)[:_CONCEPT_VOCAB_TOPN]
    return vocab, heat_map


def _gather_catalyst_news(provider: CompositeProvider, date: str,
                          tech_only: bool = False) -> list[dict]:
    """采集真实催化新闻：博查政策检索（科技赛道时用科技检索词）+ 打板题材（best-effort）。"""
    from app.strategy.detail_common import web_search
    news: list[dict] = []
    seen: set[str] = set()
    for q in (_CATALYST_TECH_QUERIES if tech_only else _CATALYST_QUERIES):
        for w in web_search(q):
            title = (w.get("title") or "").strip()
            if title and title not in seen:
                seen.add(title)
                news.append(w)
    # 打板题材作为「资金主攻方向」补充（有则附上，列名防御式取）
    kpl_themes = _kpl_themes(provider, date)
    if kpl_themes:
        news.append({"title": "今日打板资金主攻题材：" + "、".join(kpl_themes),
                     "site": "开盘啦", "date": _fmt_date(date), "summary": "", "url": ""})
    return news


def _kpl_themes(provider: CompositeProvider, date: str, top: int = 12) -> list[str]:
    """开盘啦打板榜的高频题材（反映当日资金主攻方向）。无则返回空。"""
    try:
        df = provider.get_kpl_list(date)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    col = next((c for c in ("theme", "tag", "lu_desc", "concept") if c in df.columns), None)
    if not col:
        return []
    counts: dict[str, int] = {}
    for v in df[col].dropna():
        for t in re.split(r"[+、,，/]", str(v)):
            t = t.strip()
            if len(t) >= 2:
                counts[t] = counts.get(t, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]]


def _llm_extract_catalysts(news: list[dict], vocab: list[str], heat_map: dict,
                           date: str, client) -> list:
    """LLM 从真实新闻抽取催化并映射到允许词表内的概念。client 可注入零网络单测。"""
    if not news:
        return []
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    prompt = _build_catalyst_prompt(news, vocab, heat_map, date)
    # v4-pro 是推理模型，max_tokens 同时覆盖「推理+输出」：词表大、抽取多条时推理较重，
    # 预留充足额度（实测 2200 会被推理耗尽导致正文为空，6000 稳定）。偶发空响应→重试一次。
    for _ in range(3):
        try:
            raw = client.chat([{"role": "user", "content": prompt}],
                              task_type="pro", temperature=0.3, max_tokens=_CATALYST_MAX_TOKENS)
        except Exception as e:
            logger.warning("[牛股发掘] 催化 LLM 调用失败: %s", e)
            return []
        arr = _parse_json_array(raw)
        if arr:
            return arr
    return []


def _build_catalyst_prompt(news: list[dict], vocab: list[str], heat_map: dict, date: str) -> str:
    """催化抽取 prompt：红线照搬主题解读（只引真实新闻、不臆造政策、强制 JSON、受限词表）。"""
    news_text = "\n".join(
        f"- [{n.get('date','')} {n.get('site','')}] {n.get('title','')}："
        f"{(n.get('summary') or n.get('snippet') or '')[:120]}"
        for n in news[:18])
    rising = [n for n in vocab if heat_map.get(n, {}).get("rising")]
    rising_hint = "（其中热度上升中的：" + "、".join(rising[:20]) + "）" if rising else ""
    return (
        f"你是严谨的A股政策与产业研究员。下面是 {date} 的真实新闻/题材，以及我们系统**可选的概念板块词表**。\n"
        f"任务：抽取其中**有据可查的政策/产业/题材催化**，并把每条催化映射到词表中**最相关的 1-4 个概念**。\n\n"
        f"**严格红线（违反即作废）：**\n"
        f"1. 只能依据下方真实新闻，**严禁编造或推测未出现的政策、事件、数字**；\n"
        f"2. related_concepts **只能从【概念词表】中原样挑选**，不在词表里的一律不要输出；\n"
        f"3. 不输出胜率/涨跌预测/买卖建议；evidence 必须是下方真实出现过的新闻标题。\n\n"
        f"输出严格 JSON 数组（不要代码块标记），每条：\n"
        f'{{"catalyst":"一句话催化(如:十五五规划强调新质生产力,大基金加码半导体)",'
        f'"type":"政策|新闻|题材",'
        f'"related_concepts":["必须∈词表的概念名"],'
        f'"evidence":["引用的真实新闻标题(仅取下方出现的)"]}}\n\n'
        f"最多 {_MAX_CATALYSTS} 条，按催化的确定性与时效性排序。\n\n"
        f"【真实新闻/题材】\n{news_text or '（无）'}\n\n"
        f"【概念词表（只能从这里选）】{rising_hint}\n{'、'.join(vocab)}\n\n"
        f"只输出一个 JSON 数组，不要任何额外文字。"
    )


def _normalize_catalysts(raw: list, vocab: set, heat_map: dict,
                         news: list[dict] | None = None) -> list[dict]:
    """
    清洗 LLM 催化：过滤词表外概念、补热度、把 evidence 标题回连到原始新闻(含来源/日期/可点击URL)、截断到上限。

    heat：概念有宽表数据→真实热度+rising；无数据→heat=None（前端显示「热—」而非误导的「热0」）。
    evidence：把 LLM 选的标题用相似度匹配回 news → {title,url,site,date}（匹配不到则仅留标题）。
    """
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        concepts = []
        seen = set()
        for c in (item.get("related_concepts") or []):
            name = str(c).strip()
            if name in vocab and name not in seen:   # 受限词表硬约束
                seen.add(name)
                hm = heat_map.get(name)              # 不在 heat_map=该概念无宽表热度数据
                concepts.append({"name": name,
                                 "heat": hm.get("heat") if hm else None,
                                 "rising": bool(hm.get("rising")) if hm else False})
        catalyst = str(item.get("catalyst", "")).strip()
        if not catalyst or not concepts:
            continue
        out.append({
            "catalyst": catalyst,
            "type": str(item.get("type", "")).strip() or "题材",
            "related_concepts": concepts,
            "evidence": _enrich_evidence(item.get("evidence") or [], news or []),
            "rising": any(c["rising"] for c in concepts),
        })
        if len(out) >= _MAX_CATALYSTS:
            break
    return out


def _enrich_evidence(titles: list, news: list[dict]) -> list[dict]:
    """把 LLM 引用的新闻标题回连到原始新闻（含 url/site/date·可点击溯源）。匹配不到则仅留标题。"""
    by_title = {(n.get("title") or "").strip(): n for n in news if (n.get("title") or "").strip()}
    out: list[dict] = []
    for e in titles:
        t = str(e).strip()
        if not t:
            continue
        n = by_title.get(t) or _best_news_match(t, news)
        out.append({"title": t,
                    "url": (n or {}).get("url", ""),
                    "site": (n or {}).get("site", ""),
                    "date": (n or {}).get("date", "")})
        if len(out) >= 4:
            break
    return out


def _best_news_match(title: str, news: list[dict]) -> dict | None:
    """LLM 可能轻微改写标题→用序列相似度找最接近的原始新闻（阈值 0.6，防乱配）。"""
    import difflib
    best, best_r = None, 0.0
    for n in news:
        nt = (n.get("title") or "").strip()
        if not nt:
            continue
        if title in nt or nt in title:    # 子串命中直接认
            return n
        r = difflib.SequenceMatcher(None, title, nt).ratio()
        if r > best_r:
            best, best_r = n, r
    return best if best_r >= 0.6 else None


# ══════════════════════════════════════════════════════════════════════════
# Layer 1.5 · 研报中心（博查抓媒体研报观点 → LLM 接地总结 → 映射板块·联动埋伏）
# ══════════════════════════════════════════════════════════════════════════
# ⚠️ Tushare report_rc 全局限频 1次/小时 → 不适合"及时/上量"的研报发掘；
#    本层走博查联网（媒体转述的研报观点·非全文·不限频）。诚实标注「非研报全文」。

_RESEARCH_QUERIES = [
    "A股 券商 金股 推荐 评级上调 目标价 研报",       # 个股金股 / 评级上调
    "A股 券商 行业 深度研报 景气度 看好 产业链",     # 行业 / 板块深度
    "A股 首席分析师 研报 强烈推荐 买入 机构",        # 突出分析师 / 机构
]
_MAX_REPORTS = 14            # 研报卡上限
_RESEARCH_MAX_TOKENS = 8000  # 研报卡多字段·推理较重 → 比催化层(6000)再留足，减少推理模型空响应
_RESEARCH_DISCLAIMER = (
    "研报中心 = 媒体转述的券商研报观点（非研报全文），仅供研究、不预测涨跌、不构成投资建议；"
    "研报观点≠事实，机构亦会误判。"
)


def discover_research(date: str, provider: CompositeProvider | None = None,
                      client=None, force: bool = False, tech_only: bool = False) -> dict:
    """
    博查抓媒体报道的券商研报观点 → LLM 接地总结（机构/分析师/要点/评级动作/关联板块）。

    related_concepts 只能∈我们的概念词表（供点击联动牛股发掘埋伏候选）。client 可注入零网络单测。
    tech_only：🎯 只看科技赛道（科技检索词 + 词表过滤到科技板块）。
    Returns: {ok,date,tech_only,reports:[{title,gist,org,analyst,kind,rating_action,stocks,
              related_concepts:[{name,heat,rising}],evidence:[{title,url,site,date}]}],
              disclaimer,cached,generated_at,msg}
    """
    d = (date or "").replace("-", "")
    ck = d + ("_tech" if tech_only else "")
    if not force:
        hit = _cache_get("research_hub", ck)
        if hit:
            return hit

    provider = provider or CompositeProvider()
    vocab, heat_map = _concept_vocab(provider, d)
    if tech_only:
        vocab = [n for n in vocab if _is_tech_concept(n)]
    news = _gather_research_news(tech_only)
    raw = _llm_extract_research(news, vocab, d, client) if news else []
    reports = _normalize_reports(raw, set(vocab), heat_map, news)

    result = {
        "ok": bool(reports), "date": d, "tech_only": tech_only, "reports": reports,
        "disclaimer": _RESEARCH_DISCLAIMER, "cached": False,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "msg": "" if reports else "未检索到可总结的研报观点（可能博查未配置或无结果）",
    }
    if reports:
        _cache_put("research_hub", ck, result)
    return result


def _gather_research_news(tech_only: bool = False) -> list[dict]:
    """博查抓研报相关媒体报道（科技赛道时用科技检索词·多查询去重）。无 key/无结果返回空。"""
    from app.data.web_search import BochaSearchClient
    client = BochaSearchClient()
    if not client.enabled:
        return []
    news, seen = [], set()
    for q in (_RESEARCH_TECH_QUERIES if tech_only else _RESEARCH_QUERIES):
        try:
            results = client.search(q, count=10)
        except Exception:
            results = []
        for w in results:
            t = (w.get("title") or "").strip()
            if t and t not in seen:
                seen.add(t)
                news.append(w)
    return news


def _llm_extract_research(news: list[dict], vocab: list[str], date: str, client) -> list:
    """LLM 从媒体研报报道抽取结构化卡片（client 可注入零网络单测；推理模型偶发空响应→重试一次）。"""
    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    prompt = _build_research_prompt(news, vocab, date)
    for _ in range(3):
        try:
            raw = client.chat([{"role": "user", "content": prompt}],
                              task_type="pro", temperature=0.3, max_tokens=_RESEARCH_MAX_TOKENS)
        except Exception as e:
            logger.warning("[研报中心] LLM 调用失败: %s", e)
            return []
        arr = _parse_json_array(raw)
        if arr:
            return arr
    return []


def _build_research_prompt(news: list[dict], vocab: list[str], date: str) -> str:
    """研报抽取 prompt：红线照搬（只引真实报道、不编造机构/分析师、受限词表、强制 JSON）。"""
    news_text = "\n".join(
        f"- [{n.get('date','')} {n.get('site','')}] {n.get('title','')}："
        f"{(n.get('summary') or n.get('snippet') or '')[:140]}"
        for n in news[:20])
    return (
        f"你是严谨的A股研究助理。下面是 {date} 前后媒体报道的【券商研报观点】，以及我们系统可选的概念板块词表。\n"
        f"任务：把这些**媒体转述的研报观点**整理成结构化卡片（最多 {_MAX_REPORTS} 条）。\n\n"
        f"**严格红线（违反即作废）：**\n"
        f"1. 只能依据下方真实报道，**严禁编造机构/分析师/数字/评级**；信息缺失就留空，不要瞎补；\n"
        f"2. related_concepts **只能从【概念词表】原样挑选**（不在词表的不要）；不预测涨跌、不输出胜率/必涨；\n"
        f"3. evidence 必须是下方真实出现过的标题。\n\n"
        f"输出严格 JSON 数组（不要代码块标记），每条：\n"
        f'{{"title":"研报/观点标题","gist":"1-2句核心观点(看好什么·逻辑)",'
        f'"org":"券商机构(如:中信证券,无则空字符串)","analyst":"分析师/首席(无则空字符串)",'
        f'"kind":"个股金股|评级上调|行业深度|其他",'
        f'"rating_action":"上调|首次|维持|重申|—",'
        f'"stocks":["涉及个股名(如有,无则空数组)"],'
        f'"related_concepts":["必须∈词表的概念名"],'
        f'"evidence":["引用的真实标题(仅取下方出现的)"]}}\n\n'
        f"【媒体研报报道】\n{news_text or '（无）'}\n\n"
        f"【概念词表（related_concepts 只能从这里选）】\n{'、'.join(vocab)}\n\n"
        f"只输出一个 JSON 数组，不要任何额外文字。"
    )


def _normalize_reports(raw: list, vocab: set, heat_map: dict, news: list[dict]) -> list[dict]:
    """清洗研报卡：过滤词表外概念+补热度、依据回连原文、清洗机构/分析师/个股、截断。"""
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        concepts, seen = [], set()
        for c in (item.get("related_concepts") or []):
            name = str(c).strip()
            if name in vocab and name not in seen:
                seen.add(name)
                hm = heat_map.get(name)
                concepts.append({"name": name, "heat": hm.get("heat") if hm else None,
                                 "rising": bool(hm.get("rising")) if hm else False})
        out.append({
            "title": title,
            "gist": str(item.get("gist", "")).strip(),
            "org": str(item.get("org", "")).strip(),
            "analyst": str(item.get("analyst", "")).strip(),
            "kind": str(item.get("kind", "")).strip() or "其他",
            "rating_action": str(item.get("rating_action", "")).strip() or "—",
            "stocks": [str(s).strip() for s in (item.get("stocks") or []) if str(s).strip()][:6],
            "related_concepts": concepts,
            "evidence": _enrich_evidence(item.get("evidence") or [], news),
        })
        if len(out) >= _MAX_REPORTS:
            break
    return out


# ══════════════════════════════════════════════════════════════════════════
# Layer 2 · 板块内选埋伏票层
# ══════════════════════════════════════════════════════════════════════════

def find_ambush_stocks(concept: str, date: str, provider: CompositeProvider | None = None,
                       in_catalyst: bool = True, force: bool = False) -> dict:
    """
    在某概念板块内挑选「真业绩 + 资金初入 + 还没涨太快」的埋伏候选。

    Args:
        concept:     同花顺概念名（须为 concept_members_map 的 key）。
        date:        交易日 YYYYMMDD。
        in_catalyst: 该概念是否来自催化层映射（影响催化维度评分）。
        force:       忽略缓存重算。
    Returns:
        {ok, concept, date, concept_ctx, candidates:[...], disclaimer, cached, msg}
    """
    d = _resolve_data_date((date or "").replace("-", ""))   # 埋伏需完整盘后数据→当天未入库则回退
    if not force:
        hit = _cache_get("bull_ambush", f"{d}__{concept}")
        if hit:
            return hit

    provider = provider or CompositeProvider()
    members = _concept_members(provider, concept)
    if not members:
        return {"ok": False, "concept": concept, "date": d, "candidates": [],
                "msg": f"概念「{concept}」无成分（请确认是同花顺概念名）"}

    table = _signal_table_cached(d, provider)
    if table is None or table.empty:
        return {"ok": False, "concept": concept, "date": d, "candidates": [],
                "msg": f"{d} 全市场信号表为空（非交易日或数据未就绪）"}

    sub = table[table.index.isin(members)]
    if sub.empty:
        return {"ok": False, "concept": concept, "date": d, "candidates": [],
                "msg": f"概念「{concept}」成分均不在可交易区间（市值/ST/成交额过滤后为空）"}

    concept_ctx = _concept_context(provider, concept, d, in_catalyst)
    prescreened = _prescreen(sub, _AMBUSH_PRESCREEN_TOPN)
    candidates = _score_candidates(prescreened, concept_ctx, concept, provider)
    candidates.sort(key=lambda c: c["score"], reverse=True)

    result = {
        "ok": True, "concept": concept, "date": d, "concept_ctx": concept_ctx,
        "n_members": len(members), "n_in_range": int(len(sub)),
        "n_prescreened": len(prescreened),
        "candidates": candidates[:_AMBUSH_OUTPUT_TOPN],
        "disclaimer": _DISCLAIMER, "cached": False,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "msg": "" if candidates else "该板块当前无满足三重门槛(真业绩+资金初入+位置)的埋伏标的——宁可不埋伏",
    }
    if candidates:
        _cache_put("bull_ambush", f"{d}__{concept}", result)
    return result


def _concept_members(provider: CompositeProvider, concept: str) -> list[str]:
    """取概念成分代码（容错）。"""
    try:
        from app.factors.theme_wide import concept_members_map
        return concept_members_map(provider).get(concept, [])
    except Exception:
        logger.exception("[牛股发掘] 概念成分获取失败 %s", concept)
        return []


def _signal_table_cached(date: str, provider: CompositeProvider):
    """全市场信号表（位置/资金/量价），按日缓存 parquet 供同日多概念复用。"""
    import pandas as pd
    p = get_settings().cache_dir / "bull_signal_table" / f"{date}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            return pd.read_parquet(p)
        except Exception:
            logger.debug("[牛股发掘] 信号表缓存损坏，重算 %s", date)
    from app.strategy.signals import build_signal_table
    table = build_signal_table(date, provider, min_cap_yi=_AMBUSH_MIN_CAP, max_cap_yi=_AMBUSH_MAX_CAP)
    if table is not None and not table.empty:
        try:
            table.to_parquet(p)
        except Exception as e:
            logger.debug("[牛股发掘] 信号表缓存写入失败: %s", e)
    return table


def _concept_context(provider: CompositeProvider, concept: str, date: str,
                     in_catalyst: bool) -> dict:
    """概念板块的催化上下文：热度/是否上升/资金净流入方向（供催化维度评分）。"""
    ctx = {"heat": 0.0, "rising": False, "delta": 0.0, "net_flow_in": None,
           "in_catalyst": bool(in_catalyst)}
    try:
        from app.data.theme_heat_db import get_theme, latest_trade_date
        row = get_theme(date, concept, "concept")
        if not row:                        # 该日无宽表→退到最近有热度的交易日
            ld = latest_trade_date("concept")
            row = get_theme(ld, concept, "concept") if ld else None
        if row:
            ctx["heat"] = round(_num(row.get("heat_score")) or 0.0, 1)
            ctx["delta"] = round(_num(row.get("heat_score_delta_3d")) or 0.0, 1)
            ctx["rising"] = ctx["delta"] > _RISING_DELTA_MIN
    except Exception:
        logger.debug("[牛股发掘] 概念热度上下文读取失败 %s", concept)
    ctx["net_flow_in"] = _concept_net_inflow(date, concept)
    return ctx


def _concept_net_inflow(date: str, concept: str) -> bool | None:
    """概念级资金是否净流入（同花顺 moneyflow_cnt_ths，公开口径）。失败返回 None。"""
    try:
        from app.strategy.concept_flow import build_concept_dashboard
        for r in build_concept_dashboard(date).get("rows", []):
            if r.get("concept") == concept:
                return float(r.get("net_amount", 0.0)) > 0
    except Exception:
        logger.debug("[牛股发掘] 概念资金流读取失败 %s", concept)
    return None


def _prescreen(sub, topn: int) -> list[dict]:
    """批量预筛：用位置+资金+量价（无需财务）给埋伏预分，取 Top N 交给逐只取财务。"""
    rows = []
    for ts, r in sub.iterrows():
        rec = {"ts_code": ts, **{k: r[k] for k in r.index}}
        pre = _pos_score(rec)[0] + _flow_score(rec)[0] + _vol_score(rec)[0]
        rows.append((pre, rec))
    rows.sort(key=lambda x: x[0], reverse=True)
    return [rec for _, rec in rows[:topn]]


def _score_candidates(recs: list[dict], concept_ctx: dict, concept: str,
                      provider: CompositeProvider) -> list[dict]:
    """对预筛候选逐只取真业绩+事件面，算埋伏分并过三重硬门槛（不达标剔除）。"""
    from app.strategy.fundamentals import get_financials
    out = []
    for rec in recs:
        try:
            fina = get_financials(rec["ts_code"], provider)
        except Exception:
            fina = {"ok": False}
        perf = _extract_perf(fina)
        scored = _score_ambush(rec, perf, concept_ctx, concept)
        if scored["passed"]:
            out.append(scored)
    return out


# ──────────────────────────────────────────────────────────────────────────
# 埋伏分（纯函数，可单测）
# ──────────────────────────────────────────────────────────────────────────

def _extract_perf(fina: dict) -> dict:
    """从 get_financials 结果提炼业绩证据：净利同比/ROE/业绩预告/快报/事件面。"""
    np_yoy = roe = None
    rows = (fina or {}).get("rows") or []
    if rows:
        np_yoy = _num(rows[0].get("netprofit_yoy"))
        roe = _num(rows[0].get("roe"))
    fc = (fina or {}).get("forecast") or {}
    ex = (fina or {}).get("express") or {}
    return {
        "np_yoy": np_yoy, "roe": roe,
        "forecast_level": fc.get("level"), "forecast_type": fc.get("type"),
        "express_yoy": _num(ex.get("net_profit_yoy")),
        "events": (fina or {}).get("events") or {},
        "summary": (fina or {}).get("summary", ""),
    }


def _has_real_perf(perf: dict) -> bool:
    """真业绩硬门槛：净利同比>0 / 业绩预告预增类 / 快报净利同比>0 三者有其一。"""
    if (perf.get("np_yoy") or -1) > 0:
        return True
    if perf.get("forecast_level") == "good":
        return True
    return (perf.get("express_yoy") or -1) > 0


def _score_ambush(rec: dict, perf: dict, concept_ctx: dict, concept: str) -> dict:
    """
    单只埋伏分（0-100）= 真业绩30 + 催化25 + 资金20 + 位置15 + 量能10 − 避雷扣分。

    三重硬门槛（缺一即剔除）：① 真业绩证据 ② 资金已有迹象(主力3日净流入>0) ③ 非 ST。
    第三重「催化可核」由 Layer1 真实新闻映射保证（concept_ctx.in_catalyst）。
    """
    name = str(rec.get("name", ""))
    passed, gate_reason = _hard_gate(rec, perf)

    perf_s, perf_txt = _perf_score(perf)
    cata_s, cata_txt = _cata_score(concept_ctx)
    flow_s, flow_txt = _flow_score(rec)
    pos_s, pos_txt = _pos_score(rec)
    vol_s, vol_txt = _vol_score(rec)
    penalty, flags = _event_penalty(perf.get("events") or {})

    score = round(max(0.0, perf_s + cata_s + flow_s + pos_s + vol_s - penalty), 1)
    # 双视角(0-100·重组现有维度)：真材实料=业绩+资金−避雷(实)；想象力=催化+位置+量能(未兑现的空间)
    substance = round(max(0.0, perf_s + flow_s - penalty) / (_W_PERF + _W_FLOW) * 100)
    imagination = round((cata_s + pos_s + vol_s) / (_W_CATA + _W_POS + _W_VOL) * 100)
    return {
        "ts_code": rec["ts_code"], "name": name, "passed": passed,
        "gate_reason": gate_reason,
        "score": score, "substance": substance, "imagination": imagination,
        "dims": {"perf": round(perf_s, 1), "cata": round(cata_s, 1), "flow": round(flow_s, 1),
                 "pos": round(pos_s, 1), "vol": round(vol_s, 1), "penalty": round(penalty, 1)},
        "evidence": {"perf": perf_txt, "cata": cata_txt, "flow": flow_txt,
                     "pos": pos_txt, "vol": vol_txt},
        "risk_flags": flags,
        "falsify": _falsify_text(concept),
        # 透明展示的原始因子
        "close": rec.get("close"), "pct_chg": rec.get("pct_chg"),
        "bias20": rec.get("bias20"), "dist_high": rec.get("dist_high"),
        "change_7d": rec.get("change_7d"), "main_flow_3d": rec.get("main_flow_3d"),
        "vol_ratio": rec.get("vol_ratio"), "rps50": rec.get("rps50"),
        "circ_mv_yi": rec.get("circ_mv_yi"), "turnover": rec.get("turnover"),
        "np_yoy": perf.get("np_yoy"), "roe": perf.get("roe"),
        "forecast_type": perf.get("forecast_type"), "express_yoy": perf.get("express_yoy"),
    }


def _hard_gate(rec: dict, perf: dict) -> tuple[bool, str]:
    """三重硬门槛：真业绩 + 资金初入 + 非ST（缺一剔除）。返回 (是否通过, 失败原因)。"""
    if "ST" in str(rec.get("name", "")):
        return False, "ST 风险股"
    if not _has_real_perf(perf):
        return False, "无真业绩证据(不埋伏纯题材/亏损)"
    if (_num(rec.get("main_flow_3d")) or 0.0) <= 0:
        return False, "主力近3日未净流入(不埋伏死票)"
    return True, ""


def _perf_score(perf: dict) -> tuple[float, str]:
    """真业绩 0-30：净利同比(0-15) + ROE(0-8) + 预告预增(+5) + 快报正增(+2)。"""
    np_yoy, roe = perf.get("np_yoy"), perf.get("roe")
    s = 15.0 * _ramp(np_yoy if np_yoy is not None else 0.0, 0.0, _PERF_NP_YOY_FULL)
    s += 8.0 * _ramp(roe if roe is not None else 0.0, _PERF_ROE_LO, _PERF_ROE_HI)
    if perf.get("forecast_level") == "good":
        s += 5.0
    if (perf.get("express_yoy") or 0) > 0:
        s += 2.0
    parts = []
    if np_yoy is not None:
        parts.append(f"净利同比{np_yoy:+.0f}%")
    if roe is not None:
        parts.append(f"ROE{roe:.0f}%")
    if perf.get("forecast_type"):
        parts.append(f"预告:{perf['forecast_type']}")
    return min(s, _W_PERF), "·".join(parts) or "业绩证据有限"


def _cata_score(ctx: dict) -> tuple[float, str]:
    """催化匹配 0-25：板块热度(0-12) + 热度上升(+8) + 概念资金净流入(+5)。"""
    s = 12.0 * _ramp(ctx.get("heat", 0.0), 30.0, 80.0)
    if ctx.get("rising"):
        s += 8.0
    if ctx.get("net_flow_in") is True:
        s += 5.0
    parts = [f"板块热度{ctx.get('heat', 0):.0f}"]
    if ctx.get("rising"):
        parts.append(f"热度上升Δ{ctx.get('delta', 0):+.0f}")
    if ctx.get("net_flow_in") is True:
        parts.append("概念资金净流入")
    if not ctx.get("in_catalyst", True):
        parts.append("(非催化映射板块·催化分保守)")
    return min(s, _W_CATA), "·".join(parts)


def _flow_score(rec: dict) -> tuple[float, str]:
    """资金初入 0-20：主力近3日净流入(亿) 0→满分；负流入此项为0（且硬门槛已剔除）。"""
    flow = _num(rec.get("main_flow_3d")) or 0.0
    s = _W_FLOW * _ramp(flow, 0.0, _FLOW_FULL_YI)
    return s, f"主力3日净流入{flow:+.2f}亿"


def _pos_score(rec: dict) -> tuple[float, str]:
    """未过热/低位 0-15：乖离低(0-7) + 离高点远(0-5) + 近期涨幅小(0-3)。越没涨越加分。"""
    bias = _num(rec.get("bias20")) or 0.0
    dist = _num(rec.get("dist_high")) or 0.0       # ≤0，越负离高点越远
    chg = _num(rec.get("change_7d")) or 0.0
    s = 7.0 * (1.0 - _ramp(bias, _POS_BIAS_LO, _POS_BIAS_HI))
    s += 5.0 * _ramp(-dist, 0.0, _POS_DIST_FAR)
    s += 3.0 * (1.0 - _ramp(chg, 0.0, _POS_CHG_HI))
    return min(s, _W_POS), f"乖离{bias:+.0f}%·距高{dist:+.0f}%·近7日{chg:+.0f}%"


def _vol_score(rec: dict) -> tuple[float, str]:
    """量能温和放大 0-10：太缩(没启动)/太爆(已过热)都降分，1-2倍温和放量最高。"""
    vr = _num(rec.get("vol_ratio")) or 0.0
    if vr <= 0:
        return 0.0, "量比缺失"
    if vr < _VOL_MILD_LO:                                   # 缩量：还没启动
        s = _W_VOL * (0.3 + 0.5 * _ramp(vr, 0.5, _VOL_MILD_LO))
        tag = "缩量(未启动)"
    elif vr <= _VOL_MILD_HI:                                # 温和放量：理想
        s, tag = _W_VOL, "温和放量"
    else:                                                   # 爆量：可能已过热
        s = _W_VOL * (1.0 - 0.6 * _ramp(vr, _VOL_MILD_HI, _VOL_BLOWOFF))
        tag = "放量偏大(留意过热)"
    return max(0.0, s), f"量比{vr:.1f}·{tag}"


def _event_penalty(events: dict) -> tuple[float, list[str]]:
    """避雷扣分 0-_PENALTY_MAX：解禁临近 / 大股东减持 / 大宗折价出货。"""
    penalty, flags = 0.0, []
    fl = events.get("float") or {}
    if fl.get("next_days") is not None and fl["next_days"] <= _FLOAT_NEAR_DAYS:
        ratio = fl.get("next_ratio") or 0.0
        if ratio >= _FLOAT_BIG_RATIO:
            penalty += 8.0
            flags.append(f"⚠️{fl['next_days']}天后解禁{ratio:.1f}%")
    ht = events.get("holder_trade") or {}
    if (ht.get("de_count") or 0) > 0:
        penalty += 5.0
        flags.append(f"⚠️近期减持{ht['de_count']}次")
    bl = events.get("block") or {}
    if bl.get("premium_avg") is not None and bl["premium_avg"] <= -_BLOCK_DISCOUNT:
        penalty += 5.0
        flags.append(f"⚠️大宗折价{bl['premium_avg']:.1f}%(出货)")
    return min(penalty, _PENALTY_MAX), flags


def _falsify_text(concept: str) -> str:
    """埋伏的催化证伪止损条件（诚实红线：证伪即走）。"""
    return (f"证伪止损：若「{concept}」催化落空(政策不及预期/题材退潮)、"
            f"或主力转净流出、或跌破关键均线(MA20)，则埋伏逻辑破坏，离场。")


# ──────────────────────────────────────────────────────────────────────────
# 杂项
# ──────────────────────────────────────────────────────────────────────────

def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
