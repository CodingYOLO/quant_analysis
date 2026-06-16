"""
博查 Bocha 联网搜索客户端（为 LLM 提供真实、最新的网络检索结果）。

定位：与 LLMClient 同级的独立能力客户端（不混入行情 DataProvider）。
用途：行业详情的「驱动点评」在生成前，先用博查搜本行业的最新真实新闻，
      把「标题+摘要+来源+时间+URL」喂给 LLM 作为可溯源依据，杜绝凭空臆造。

API（已核对端点与请求体；响应按 Bing 兼容结构做防御式解析）：
  POST https://api.bochaai.com/v1/web-search
  Header: Authorization: Bearer <key>
  Body:   {"query","freshness","summary","count"}
  Resp:   data.webPages.value[] → {name,url,snippet,summary,siteName,datePublished}

未配置 key 时所有方法安全返回空，调用方据此自动降级（不联网）。
真实字段以 verify_connection() 跑通结果为准（见 CLI: bocha-check）。
"""

from __future__ import annotations

import logging

import requests

from app.config import get_settings

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.bochaai.com/v1/web-search"
_TIMEOUT = 12.0


class BochaSearchClient:
    """博查 Web Search API 封装。无 key 时降级为空结果。"""

    def __init__(self, api_key: str | None = None, freshness: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.bocha_api_key
        self._freshness = freshness or settings.bocha_freshness

    @property
    def enabled(self) -> bool:
        """是否已配置 key（未配置则全链路降级，不联网）。"""
        return bool(self._api_key)

    def search(self, query: str, count: int = 8, freshness: str | None = None) -> list[dict]:
        """
        执行一次联网搜索，返回规范化结果列表。

        Args:
            query:     查询词
            count:     返回条数（1-50）
            freshness: 时效，覆盖默认；取值 oneDay/oneWeek/oneMonth/oneYear/noLimit

        Returns:
            list[dict]，每条含 title/url/snippet/summary/site/date；
            未启用或失败时返回 []（调用方据此降级）。
        """
        if not self.enabled or not query.strip():
            return []

        payload = {
            "query": query.strip(),
            "freshness": freshness or self._freshness,
            "summary": True,
            "count": max(1, min(count, 50)),
        }
        try:
            resp = requests.post(
                _ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning("[博查] HTTP%d：%s", resp.status_code, resp.text[:200])
                return []
            return self._parse(resp.json())
        except Exception as e:
            logger.warning("[博查] 搜索失败（降级为不联网）: %s", e)
            return []

    @staticmethod
    def _parse(data: dict) -> list[dict]:
        """
        防御式解析博查响应（Bing 兼容）。
        兼容 data 包裹层缺失、字段缺失等情况，任意异常返回 []。
        """
        try:
            root = data.get("data") or data            # 兼容有/无 data 包裹
            pages = (root.get("webPages") or {}).get("value") or []
            out = []
            for p in pages:
                if not isinstance(p, dict):
                    continue
                out.append({
                    "title": str(p.get("name", "")).strip(),
                    "url": str(p.get("url", "")).strip(),
                    "snippet": str(p.get("snippet", "")).strip(),
                    "summary": str(p.get("summary", "") or "").strip(),
                    "site": str(p.get("siteName", "") or "").strip(),
                    "date": str(p.get("datePublished", "") or "").strip()[:10],
                })
            return [r for r in out if r["title"]]
        except Exception as e:
            logger.warning("[博查] 响应解析失败: %s", e)
            return []

    def verify_connection(self) -> dict:
        """
        健康检查：用固定查询打一次接口，确认 key 有效、响应结构符合预期。
        供 CLI `bocha-check` 调用。

        Returns:
            dict(ok, detail, sample) — sample 为首条结果，便于核对真实字段。
        """
        if not self.enabled:
            return {"ok": False, "detail": "未配置 BOCHA_API_KEY", "sample": None}
        results = self.search("A股 半导体 行业 最新消息", count=3, freshness="oneWeek")
        if results:
            return {"ok": True, "detail": f"取到 {len(results)} 条", "sample": results[0]}
        return {"ok": False, "detail": "无结果或解析为空（请核对 key 或响应结构）", "sample": None}
