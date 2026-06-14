"""
节点 B: 板块热度评分 + 新闻主题分析（Phase 2）。

两条子流水线并行执行，结果合并写入 state：
  1. 量化板块热度（sector_stats） ← Phase 2 第一步（已完成）
  2. LLM新闻主题线（themes）      ← Phase 2 第二步（本次实现）
     财联社新闻 → DeepSeek-flash批量打标 → 主题聚合 → 行业映射 → 事件动量标记

Node C 将使用 state.themes 中的 event_catalyst_industries
给候选股打事件动量加分（+20分）。
"""

import json
import logging
import re
from pathlib import Path

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix
from app.factors import calc_rps
from app.llm.client import LLMClient
from app.sector_analyzer import calc_sector_stats
from app.state import PipelineState, Theme, ThemeLeader

logger = logging.getLogger(__name__)

_HOT_THRESHOLD = 50.0
_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "theme_scoring.txt"

# 主题关键词 → A股行业映射（用于将LLM输出的主题名匹配到 stock_basic.industry）
# 格式：{LLM可能输出的主题关键词: [行业名关键词列表]}
_THEME_TO_INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "半导体": ["半导体", "元器件", "集成电路"],
    "芯片": ["半导体", "元器件"],
    "新能源": ["光伏", "储能", "风电", "电池", "新能源"],
    "储能": ["储能", "电池"],
    "光伏": ["光伏", "太阳能"],
    "人工智能": ["计算机", "软件", "通信设备", "电子"],
    "AI": ["计算机", "软件", "通信设备"],
    "军工": ["国防军工", "航空航天", "兵器"],
    "航空": ["航空航天", "通用设备"],
    "医药": ["医药生物", "化学制药", "中药", "医疗器械"],
    "生物": ["医药生物", "生物制品"],
    "消费": ["食品饮料", "零售", "餐饮旅游", "纺织服装"],
    "白酒": ["食品饮料"],
    "新能源车": ["汽车", "汽车零部件", "电池"],
    "汽车": ["汽车", "汽车零部件"],
    "稀土": ["稀土", "小金属", "有色金属"],
    "低空经济": ["航空航天", "通用设备", "通信设备"],
    "机器人": ["机械设备", "通用设备", "电子"],
    "银行": ["银行"],
    "保险": ["保险"],
    "券商": ["证券"],
    "房地产": ["房地产"],
    "建筑": ["建筑", "建材"],
    "化工": ["化工", "基础化工"],
    "煤炭": ["煤炭开采", "焦炭加工"],
    "钢铁": ["钢铁"],
    "有色": ["有色金属", "小金属"],
}


def node_theme_analysis(state: PipelineState) -> PipelineState:
    """板块热度评分 + 新闻主题分析 + 主题龙头股关联 三流水线。"""
    logger.info("[节点B] 板块热度评分 + LLM新闻主题分析（Phase 2）")

    provider = CompositeProvider()
    close_m = _load_close_matrix(state.trade_date, provider)

    # ---- 子流水线1：量化板块热度 ----
    if close_m is not None:
        state.sector_stats = calc_sector_stats(
            trade_date=state.trade_date,
            provider=provider,
            close_m=close_m,
        )
        _log_sector_summary(state.sector_stats)
    else:
        state.sector_stats = []
        logger.warning("[节点B] 价格矩阵加载失败，跳过量化板块热度")

    # ---- 子流水线2：LLM新闻主题分析 ----
    themes = _run_news_theme_pipeline(state.trade_date, provider, state.sector_stats)

    # ---- 子流水线3：主题龙头股关联（步骤10）----
    if themes and close_m is not None:
        themes = _attach_theme_leaders(themes, state.trade_date, provider, close_m)

    state.themes = themes
    return state


# ──────────────────────────────────────────────
# 量化板块热度（子流水线1）
# ──────────────────────────────────────────────

def _load_close_matrix(trade_date: str, provider: CompositeProvider):
    """加载25日收盘价矩阵（MA20计算最少需要21日）。"""
    try:
        close_m, *_ = load_price_matrix(trade_date, provider, n_days=25)
        return close_m
    except Exception as e:
        logger.error("加载价格矩阵失败: %s", e)
        return None


def _log_sector_summary(sector_stats: list) -> None:
    hot = [s for s in sector_stats if s.heat_score > _HOT_THRESHOLD]
    decay = [s for s in sector_stats if s.phase == "退潮"]
    logger.info(
        "[节点B] 行业共%d个 | 热门板块%d个 | 退潮预警%d个",
        len(sector_stats), len(hot), len(decay),
    )
    for s in hot[:5]:
        logger.info(
            "  板块[%s] 热度%.1f 阶段=%s 5日资金%.1f亿 广度%.0f%% 连板%d板",
            s.industry, s.heat_score, s.phase,
            s.flow_5d_100m, s.pct_above_ma20 * 100, s.consecutive_limit_high,
        )


# ──────────────────────────────────────────────
# LLM新闻主题分析（子流水线2）
# ──────────────────────────────────────────────

def _run_news_theme_pipeline(
    trade_date: str,
    provider: CompositeProvider,
    sector_stats: list,
) -> list[Theme]:
    """
    完整新闻主题分析流水线：
    拉取新闻 → DeepSeek-flash打标 → 主题聚合 → 行业映射 → 返回Theme列表
    """
    # 1. 拉取财联社新闻
    news_items = _fetch_news(trade_date, provider)
    if not news_items:
        logger.info("[节点B] 无财联社新闻（历史日期或接口不可用），跳过LLM主题分析")
        return []

    logger.info("[节点B] 获取财联社新闻 %d 条，开始LLM打标", len(news_items))

    # 2. DeepSeek-flash 批量打标
    tagged = _batch_score_news(news_items)
    if not tagged:
        logger.warning("[节点B] LLM打标结果为空，跳过主题分析")
        return []

    # 3. 聚合主题
    raw_themes = _aggregate_themes(news_items, tagged)

    # 4. 映射行业 + 与量化板块热度合并
    themes = _build_theme_objects(raw_themes, sector_stats)

    logger.info("[节点B] 识别出主题 %d 个（热门: %d 个）",
                len(themes), sum(1 for t in themes if t.heat >= 6))
    for t in sorted(themes, key=lambda x: x.heat, reverse=True)[:5]:
        logger.info("  主题[%s] 热度%.1f %s 行业映射:%s",
                    t.name, t.heat, t.phase, ",".join(t.concept_codes[:3]))

    return themes


def _fetch_news(trade_date: str, provider: CompositeProvider) -> list[dict]:
    """
    拉取财联社电报 + 东方财富财经要闻，返回 [{text, time}] 列表。
    优先使用缓存；接口不可用时静默返回空列表。
    """
    try:
        df = provider.get_cls_news(trade_date)
        if df is None or df.empty:
            return []

        # 东方财富字段：标题 / 摘要 / 发布时间
        title_col = _find_col(df, ["标题", "摘要", "新闻标题", "title", "内容", "content"])
        time_col = _find_col(df, ["发布时间", "时间", "发布日期", "time", "date"])

        if title_col is None:
            logger.debug("财联社新闻字段无法识别，列名: %s", list(df.columns))
            return []

        items = []
        for _, row in df.head(60).iterrows():  # 最多取60条
            text = str(row.get(title_col, "")).strip()
            if len(text) < 10:
                continue
            items.append({
                "text": text,
                "time": str(row.get(time_col, "")) if time_col else "",
            })
        return items

    except Exception as e:
        logger.info("[节点B] 财联社新闻获取失败（%s），跳过LLM分析", e)
        return []


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """在 DataFrame 中找到第一个存在的候选列名。"""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _batch_score_news(news_items: list[dict]) -> list[dict]:
    """
    批量调用 DeepSeek-flash 对新闻打标，返回解析后的打标结果列表。
    每批20条，LLM输出JSON数组。
    """
    prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
    llm = LLMClient()

    all_results: list[dict] = []
    batch_size = 20

    for batch_start in range(0, len(news_items), batch_size):
        batch = news_items[batch_start: batch_start + batch_size]
        numbered = "\n".join(
            f"{i+1}. {item['text']}" for i, item in enumerate(batch)
        )
        # 用 replace 而非 format，避免 prompt 中的 JSON {} 被误当占位符
        prompt = prompt_template.replace("{items}", numbered)

        try:
            raw = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                task_type="flash",
                temperature=0.1,
                max_tokens=2000,
            )
            parsed = _parse_llm_json(raw)
            # 修正 idx 偏移（每批从1开始，需加上 batch_start）
            for item in parsed:
                item["_batch_idx"] = batch_start + item.get("idx", 1) - 1
            all_results.extend(parsed)
        except Exception as e:
            logger.warning("LLM批次%d打标失败: %s", batch_start // batch_size + 1, e)

    return all_results


def _parse_llm_json(raw: str) -> list[dict]:
    """
    从 LLM 输出中提取并解析 JSON 数组。
    容忍：markdown 代码块包裹、key 含换行/引号、非标准格式。
    """
    # 去掉 markdown 代码块
    text = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("```").strip()

    # 找到第一个 [ 到最后一个 ]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []

    try:
        items = json.loads(text[start: end + 1])
    except json.JSONDecodeError as e:
        logger.debug("JSON解析失败: %s | raw前200字: %s", e, raw[:200])
        return []

    # 规范化：过滤非dict项，清洗 key 中可能含有的换行/多余引号
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        clean = {k.strip().strip('"'): v for k, v in item.items()}
        result.append(clean)
    return result


def _aggregate_themes(
    news_items: list[dict],
    tagged: list[dict],
) -> list[dict]:
    """
    将打标结果按主题聚合，计算：
    - 主题总热度（各条新闻热度求和）
    - 是否有事件驱动型催化
    - 证据摘要列表
    """
    theme_map: dict[str, dict] = {}

    for tag in tagged:
        idx = tag.get("_batch_idx", 0)
        themes = tag.get("themes", [])
        heat = float(tag.get("heat", 1))
        is_event = bool(tag.get("is_event_driven", False))
        evidence = str(tag.get("evidence", ""))

        for theme_name in themes:
            if not theme_name:
                continue
            if theme_name not in theme_map:
                theme_map[theme_name] = {
                    "name": theme_name,
                    "total_heat": 0.0,
                    "article_count": 0,
                    "has_event_catalyst": False,
                    "evidence_list": [],
                }
            entry = theme_map[theme_name]
            entry["total_heat"] += heat
            entry["article_count"] += 1
            entry["has_event_catalyst"] |= is_event
            if evidence and len(entry["evidence_list"]) < 3:
                entry["evidence_list"].append(evidence)

    # 归一化热度到 0~10 区间
    if theme_map:
        max_heat = max(e["total_heat"] for e in theme_map.values())
        for entry in theme_map.values():
            entry["normalized_heat"] = round(
                entry["total_heat"] / max(max_heat, 1) * 10, 1
            )

    return sorted(theme_map.values(), key=lambda x: x["total_heat"], reverse=True)


def _build_theme_objects(
    raw_themes: list[dict],
    sector_stats: list,
) -> list[Theme]:
    """
    将聚合结果转为 Theme 对象，映射对应行业代码，并用量化板块热度补充 phase。
    """
    # 量化热度快查：{industry: phase}
    sector_phase = {s.industry: s.phase for s in sector_stats}
    sector_heat = {s.industry: s.heat_score for s in sector_stats}

    themes: list[Theme] = []
    for raw in raw_themes[:15]:  # 最多保留15个主题
        name = raw["name"]
        heat = raw.get("normalized_heat", raw.get("total_heat", 0.0))

        # 热度 < 3 且无事件催化 → 忽略
        if heat < 3.0 and not raw.get("has_event_catalyst"):
            continue

        # 映射行业（用于 Node C 事件动量加分）
        mapped_industries = _map_theme_to_industries(name, list(sector_phase.keys()))

        # 取量化板块阶段作为辅助（优先用量化数据）
        quant_phase = _get_dominant_phase(mapped_industries, sector_phase)
        phase = _determine_theme_phase(
            heat=heat,
            has_event=raw.get("has_event_catalyst", False),
            quant_phase=quant_phase,
        )

        # 量化热度加成（如果对应板块热度也高，合并提升）
        quant_boost = _get_quant_heat_boost(mapped_industries, sector_heat)
        final_heat = min(round(heat + quant_boost, 1), 10.0)

        evidence = raw.get("evidence_list", [])
        if raw.get("has_event_catalyst") and evidence:
            evidence = ["⚡ 事件催化"] + evidence

        themes.append(Theme(
            name=name,
            heat=final_heat,
            phase=phase,
            evidence=evidence,
            concept_codes=mapped_industries,  # 复用字段存行业名列表
        ))

    return themes


def _map_theme_to_industries(theme_name: str, all_industries: list[str]) -> list[str]:
    """
    将 LLM 输出的主题名映射到 stock_basic.industry 行业名称列表。
    先查预设字典，再用关键词模糊匹配已知行业。
    """
    matched: set[str] = set()

    # 查预设映射表
    for keyword, industries in _THEME_TO_INDUSTRY_KEYWORDS.items():
        if keyword in theme_name:
            for ind_kw in industries:
                for real_ind in all_industries:
                    if ind_kw in real_ind:
                        matched.add(real_ind)

    # 如果主题名直接包含已知行业名（精确匹配优先）
    for ind in all_industries:
        if ind in theme_name or theme_name in ind:
            matched.add(ind)

    return list(matched)


def _get_dominant_phase(industries: list[str], sector_phase: dict[str, str]) -> str | None:
    """取映射行业中占主导的量化阶段（升温>趋势>中性>退潮）。"""
    phase_priority = {"升温": 4, "趋势": 3, "中性": 2, "退潮": 1}
    best = None
    best_score = 0
    for ind in industries:
        phase = sector_phase.get(ind, "中性")
        score = phase_priority.get(phase, 0)
        if score > best_score:
            best_score = score
            best = phase
    return best


def _determine_theme_phase(heat: float, has_event: bool, quant_phase: str | None) -> str:
    """综合 LLM 热度、事件催化、量化阶段判断主题阶段。"""
    if quant_phase == "退潮":
        return "退潮"
    if has_event and heat >= 7:
        return "事件驱动↑"
    if heat >= 6 or quant_phase == "升温":
        return "升温"
    if heat >= 4 or quant_phase == "趋势":
        return "趋势"
    return "中性"


def _get_quant_heat_boost(industries: list[str], sector_heat: dict[str, float]) -> float:
    """
    如果对应量化板块热度高，给 LLM 热度轻微加成（最多+1分）。
    避免过度依赖量化或LLM任意一方。
    """
    if not industries:
        return 0.0
    max_quant = max(sector_heat.get(ind, 0) for ind in industries)
    if max_quant > 80:
        return 1.0
    elif max_quant > 70:
        return 0.5
    return 0.0


# ──────────────────────────────────────────────
# 步骤10：主题龙头股关联
# ──────────────────────────────────────────────

def _attach_theme_leaders(
    themes: list[Theme],
    trade_date: str,
    provider: CompositeProvider,
    close_m,
) -> list[Theme]:
    """
    为每个热门主题找出 Top3 龙头股。
    筛选标准（按优先级）：
      1. 所属行业在主题关联行业内
      2. 当日涨幅排名靠前（动量领先）
      3. 有主力资金净流入
    """
    try:
        daily = provider.get_daily(trade_date)
        money_flow = provider.get_money_flow(trade_date)
        stock_basic = provider.get_stock_basic()
        rps50 = calc_rps(close_m, n=50)

        if daily is None or daily.empty or stock_basic is None:
            return themes

        # 合并行情 + 行业 + 资金流
        merged = daily[["ts_code", "pct_chg", "amount"]].merge(
            stock_basic[["ts_code", "name", "industry"]], on="ts_code", how="left"
        )
        if money_flow is not None and not money_flow.empty:
            mf = money_flow[["ts_code", "buy_elg_amount", "sell_elg_amount",
                             "buy_lg_amount", "sell_lg_amount"]].copy()
            mf["main_net"] = (
                (mf["buy_elg_amount"] - mf["sell_elg_amount"]) +
                (mf["buy_lg_amount"] - mf["sell_lg_amount"])
            )
            merged = merged.merge(mf[["ts_code", "main_net"]], on="ts_code", how="left")
        else:
            merged["main_net"] = 0.0

        merged["rps50"] = merged["ts_code"].map(rps50).fillna(50)
        merged["main_net"] = merged["main_net"].fillna(0)
        merged = merged.dropna(subset=["industry"])

    except Exception as e:
        logger.warning("[节点B] 主题龙头股数据加载失败: %s", e)
        return themes

    _LIMIT_UP = 9.5
    for theme in themes:
        if theme.heat < 4.0 or not theme.concept_codes:
            continue

        # 筛选关联行业的股票
        sector_stocks = merged[merged["industry"].isin(theme.concept_codes)].copy()
        if sector_stocks.empty:
            continue

        # 综合评分：涨幅(40%) + RPS(30%) + 主力资金(30%)
        sector_stocks["score"] = (
            sector_stocks["pct_chg"].clip(-10, 10) / 10 * 40
            + sector_stocks["rps50"] / 100 * 30
            + sector_stocks["main_net"].apply(
                lambda x: 30 if x > 10000 else (15 if x > 0 else 0)
            )
        )

        top3 = sector_stocks.nlargest(3, "score")
        leaders = []
        for _, row in top3.iterrows():
            leaders.append(ThemeLeader(
                code=row["ts_code"],
                name=str(row.get("name", "")),
                pct_change=round(float(row["pct_chg"]), 2),
                rps50=round(float(row["rps50"]), 0),
                fund_flow=round(float(row.get("main_net", 0)), 0),
                is_limit_up=float(row["pct_chg"]) >= _LIMIT_UP,
            ))
        theme.leaders = leaders

        if leaders:
            logger.info(
                "  主题[%s] 龙头: %s",
                theme.name,
                " / ".join(f"{l.name}{l.pct_change:+.1f}%" for l in leaders),
            )

    return themes
