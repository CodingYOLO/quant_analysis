"""
行业 / 概念 详情面板的共享底层逻辑（DRY）。

被 industry_detail 与 concept_detail 共用，集中三类能力：
  - 真实信息源采集：成分股公告、当日大盘新闻、博查联网检索
  - 新闻按主体精筛（杜绝 LLM 拿无关新闻附会编造）
  - 接地式 LLM 点评合成（硬性禁止编造，段末强制列「依据」）
  - 详情 JSON 缓存（按 种类/日期/主体 落盘，含 LLM 结果，避免重复调用）

设计原则：本模块只做「主体无关」的通用处理；主体特有的资金/成分/题材聚合
留在各自的 industry_detail / concept_detail 中，保持低耦合。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

_MAX_NOTICES = 8
_MAX_HEADLINE_POOL = 120   # 全市场新闻候选池上限
_MAX_REL_NEWS = 12         # 精筛后喂 LLM 的相关新闻上限
_MAX_WEB = 6               # 博查联网检索条数


# ──────────────────────────────────────────────
# 文本工具
# ──────────────────────────────────────────────

def lead_names(lead: str) -> list[str]:
    """从领涨股串「珠海冠宇(002...)+5.0%、…」中提取股票名。"""
    return [m.strip() for m in re.findall(r"([一-龥A-Za-z]{2,8})\(", lead or "")]


# ──────────────────────────────────────────────
# 真实信息源采集
# ──────────────────────────────────────────────

def notices_for_symbols(provider: CompositeProvider, date: str, symbols: set[str]) -> list[str]:
    """指定成分股（6位代码集合）当日重大公告，格式化为字符串列表。"""
    try:
        df = provider._ak.get_company_notices(date)  # 默认仅高影响类型
    except Exception as e:
        logger.debug("[详情] 公告拉取失败: %s", e)
        return []
    if df is None or df.empty or "代码" not in df.columns:
        return []

    sub = df[df["代码"].astype(str).isin(symbols)]
    out = []
    for _, r in sub.head(_MAX_NOTICES).iterrows():
        name = str(r.get("名称", "")).strip()
        code = str(r.get("代码", "")).strip()
        ntype = str(r.get("公告类型", "")).strip()
        title = str(r.get("公告标题", "")).strip()[:60]
        out.append(f"{name}({code}) [{ntype}] {title}")
    return out


def macro_headlines(provider: CompositeProvider, date: str) -> list[str]:
    """当日全市场真实新闻标题（财联社电报/东财），作为精筛候选池。"""
    try:
        df = provider.get_cls_news(date)
    except Exception:
        return []
    if df is None or df.empty or "标题" not in df.columns:
        return []
    return [str(t).strip() for t in df["标题"].head(_MAX_HEADLINE_POOL) if str(t).strip()]


def relevant_news(headlines: list[str], keys: set[str]) -> list[str]:
    """只保留标题中确实出现 keys（主体名/龙头股名/题材名）的真实新闻。"""
    keys = {k for k in keys if k and len(k) >= 2}
    seen, out = set(), []
    for h in headlines:
        if any(k in h for k in keys) and h not in seen:
            seen.add(h)
            out.append(h[:90])
        if len(out) >= _MAX_REL_NEWS:
            break
    return out


def web_search(subject: str, lead_kw: str = "") -> list[dict]:
    """
    博查联网检索主体的最新真实新闻（含来源/日期/URL）。
    未配置 BOCHA_API_KEY 时返回空，调用方自动降级为不联网。
    """
    try:
        from app.data.web_search import BochaSearchClient
        client = BochaSearchClient()
        if not client.enabled:
            return []
        query = f"A股 {subject} 最新动态 政策 消息 {lead_kw}".strip()
        return client.search(query, count=_MAX_WEB)
    except Exception as e:
        logger.debug("[详情] 联网检索失败: %s", e)
        return []


# ──────────────────────────────────────────────
# 接地式 LLM 点评合成
# ──────────────────────────────────────────────

def compose_comment(
    *,
    subject: str,
    fund_summary: str,
    lead: str,
    notices: list[str],
    micro_label: str,
    micro_text: str,
    rel_news: list[str],
    web: list[dict],
) -> str:
    """
    生成接地式驱动点评（严禁编造信息源外的内容）。

    Args:
        subject:      主体描述，如「电气设备行业」「芯片概念」
        fund_summary: 资金情绪定性句
        lead:         领涨股/成分股串
        notices:      成分股重大公告
        micro_label:  微观催化板块标签，如「关联活跃题材」「领涨成分股」
        micro_text:   对应内容文本
        rel_news:     精筛后的相关新闻
        web:          博查联网检索结果
    """
    notices_text = "\n".join(f"- {n}" for n in notices) or "（无）"
    news_text = "\n".join(f"- {h}" for h in rel_news) or "（财联社电报中无本主体直接相关条目）"
    web_text = "\n".join(
        f"- [{w.get('date','')} {w.get('site','')}] {w.get('title','')}："
        f"{(w.get('summary') or w.get('snippet') or '')[:140]}"
        for w in web
    ) or "（未启用联网检索或无结果）"

    prompt = (
        f"你是严谨的A股研究员。**只能依据下方提供的「真实信息源」作答，"
        f"严禁编造或推测任何未在信息源中出现的公司、数字、事件、政策或价格。**"
        f"若某一层面缺乏对应信息源，必须直接写明该层面「暂无公开信息」，不得自行补全或想象。\n\n"
        f"请就【{subject}】写一段 100-180 字连贯点评（不分点），覆盖三层：\n"
        f"①盘面/资金强弱——依据【资金面】；\n"
        f"②微观催化——仅依据【重大公告】【{micro_label}】，点名的公司/题材必须在下方出现过；\n"
        f"③消息/政策面——依据【相关新闻】与【联网检索】；二者都为空时写「今日暂无直接相关消息面」。\n"
        f"要求：客观中性、不预测涨跌、不给买卖建议、不输出胜率或排名。\n"
        f"正文之后另起一行，以「依据：」列出实际引用的信息源类别（资金面/公告/{micro_label}/新闻/联网）。\n\n"
        f"━━ 真实信息源 ━━\n"
        f"【资金面】{fund_summary}\n"
        f"【领涨/龙头】{lead or '（无）'}\n"
        f"【重大公告】\n{notices_text}\n"
        f"【{micro_label}】\n{micro_text or '（无）'}\n"
        f"【相关新闻（财联社电报，已精筛）】\n{news_text}\n"
        f"【联网检索（博查，真实网页，含来源与日期）】\n{web_text}\n"
    )
    try:
        return LLMClient().chat(
            [{"role": "user", "content": prompt}],
            task_type="pro",
            temperature=0.2,
            max_tokens=1300,
        ).strip()
    except Exception as e:
        logger.warning("[详情] LLM 点评生成失败: %s", e)
        return "（驱动点评暂不可用：LLM 调用失败，请稍后重试）"


# ──────────────────────────────────────────────
# 缓存
# ──────────────────────────────────────────────

def cache_path(kind: str, date: str, key: str) -> Path:
    """详情缓存路径。kind 区分 industry/concept；key 为主体名，做文件名安全化。"""
    safe = re.sub(r"[^\w一-龥]+", "_", key)
    d = get_settings().cache_dir / f"{kind}_detail"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{date}__{safe}.json"


def load_cache(path: Path) -> dict | None:
    """读缓存；命中时 cached 置 True。失败返回 None。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["cached"] = True
        return data
    except Exception:
        logger.debug("[详情] 缓存读取失败，重算: %s", path)
        return None


def save_cache(path: Path, data: dict) -> None:
    """落盘缓存（cached 字段不入库）。"""
    try:
        payload = {k: v for k, v in data.items() if k != "cached"}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("[详情] 缓存写入失败: %s", e)
