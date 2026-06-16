"""
个股网络舆情避雷：用博查联网检索候选股的最新负面消息。

准确性优先设计：
  - 结果以「真实新闻原文（标题+来源+日期+URL）」呈现，不经 LLM 复述加工，
    从根上杜绝失真；命中与否由确定性关键词匹配决定。
  - 必须同时满足「标题/摘要中出现该股名」+「命中风险关键词」才计为预警，
    宁可漏报也不误报（误报一只好票的代价更高）。
  - 与现有 Tushare 结构化避雷（立案/减持/业绩预告）互补：博查能抓到
    尚未进结构化数据库的最新网络舆情。

未配置 BOCHA_API_KEY 时整体降级（返回空），不影响选股报告生成。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 风险关键词（命中即视为潜在负面，需人工核实）
_RISK_KEYWORDS = [
    "立案", "调查", "处罚", "违规", "违法", "问询", "关注函", "警示函",
    "减持", "清仓", "退市", "*ST", "ST", "诉讼", "仲裁", "冻结",
    "质押", "爆仓", "商誉减值", "预亏", "预减", "首亏", "续亏", "亏损",
    "造假", "监管", "处分", "停牌", "核查", "举报", "踩雷", "暴雷",
]

_MAX_HITS_PER_STOCK = 3


def scan_candidates(candidates: list, freshness: str = "oneMonth", per_stock: int = 6) -> dict:
    """
    逐只候选股博查检索最新负面舆情。

    Args:
        candidates: 候选股列表（需含 .name / .code 属性）
        freshness:  搜索时效，默认近一月
        per_stock:  每只股票检索条数

    Returns:
        dict: {code6: [hit, ...]}，仅包含有命中的股票；
              hit = {title, url, site, date, keyword}。
              未启用博查时返回 {}。
    """
    try:
        from app.data.web_search import BochaSearchClient
        client = BochaSearchClient()
    except Exception as e:
        logger.debug("[避雷] 博查客户端初始化失败: %s", e)
        return {}
    if not client.enabled or not candidates:
        return {}

    result: dict[str, list[dict]] = {}
    for c in candidates:
        name = getattr(c, "name", "")
        code6 = str(getattr(c, "code", ""))[:6]
        if not name:
            continue
        hits = _scan_one(client, name, freshness, per_stock)
        if hits:
            result[code6] = hits
    if result:
        logger.info("[避雷] 博查检索命中负面的候选股: %d 只", len(result))
    return result


def _scan_one(client, name: str, freshness: str, per_stock: int) -> list[dict]:
    """对单只股票检索并做确定性风险过滤。"""
    query = f"{name} 股票 减持 立案 问询 诉讼 处罚 业绩 风险 公告"
    try:
        results = client.search(query, count=per_stock, freshness=freshness)
    except Exception as e:
        logger.debug("[避雷] 检索失败 %s: %s", name, e)
        return []

    hits = []
    for r in results:
        title = str(r.get("title", ""))
        body = str(r.get("summary") or r.get("snippet") or "")
        text = f"{title} {body}"
        # 双重约束：必须提到该股名 + 命中风险关键词（降误报）
        if name not in text:
            continue
        kw = next((k for k in _RISK_KEYWORDS if k in text), None)
        if not kw:
            continue
        hits.append({
            "title": title[:90],
            "url": str(r.get("url", "")),
            "site": str(r.get("site", "")),
            "date": str(r.get("date", "")),
            "keyword": kw,
        })
        if len(hits) >= _MAX_HITS_PER_STOCK:
            break
    return hits
