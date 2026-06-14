"""
LLMClient: 统一的 LLM 调用入口。
- 支持 DeepSeek / Claude 切换（OpenAI 兼容接口）
- 内置超时、重试、调用日志（记录 token 消耗与预估费用）
- 批量调用接口，避免一条消息一个请求
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, date
from pathlib import Path
from typing import Literal

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from app.config import get_settings

logger = logging.getLogger(__name__)

# DeepSeek 价格（人民币/1M tokens，2024年参考价，可在 .env 覆盖）
_DEEPSEEK_PRICE_INPUT = 0.5   # ¥/1M input tokens (flash)
_DEEPSEEK_PRICE_OUTPUT = 2.0  # ¥/1M output tokens (flash)
_DEEPSEEK_PRO_PRICE_INPUT = 4.0
_DEEPSEEK_PRO_PRICE_OUTPUT = 16.0

TaskType = Literal["flash", "pro"]


class LLMClient:
    """
    统一 LLM 调用客户端，支持 deepseek / claude provider 切换。
    通过 task_type 自动选择合适的模型（flash=高频低价值，pro=低频高价值）。
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._settings = get_settings()
        self._db_path = db_path or (self._settings.cache_dir / "llm_cost.db")
        self._init_db()

    def _init_db(self) -> None:
        """初始化调用日志数据库。"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    task_type TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cost_cny REAL,
                    elapsed_ms INTEGER
                )
            """)

    def _get_client(self, task_type: TaskType) -> tuple[OpenAI, str]:
        """
        根据 task_type 和配置返回 (OpenAI客户端, model名称)。
        """
        provider = self._settings.llm_provider

        if provider == "deepseek":
            client = OpenAI(
                api_key=self._settings.deepseek_api_key,
                base_url="https://api.deepseek.com",
                timeout=60.0,
            )
            model = (
                self._settings.deepseek_flash_model
                if task_type == "flash"
                else self._settings.deepseek_pro_model
            )
        elif provider == "claude":
            # Claude 也支持 OpenAI 兼容接口（通过 anthropic SDK 或兼容层）
            client = OpenAI(
                api_key=self._settings.claude_api_key,
                base_url="https://api.anthropic.com/v1",
                timeout=120.0,
            )
            model = self._settings.claude_model
        else:
            raise ValueError(f"不支持的 LLM provider: {provider}")

        return client, model

    def _estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """估算本次调用费用（人民币）。"""
        if "reasoner" in model or "pro" in model.lower():
            cost = (input_tokens * _DEEPSEEK_PRO_PRICE_INPUT + output_tokens * _DEEPSEEK_PRO_PRICE_OUTPUT) / 1_000_000
        else:
            cost = (input_tokens * _DEEPSEEK_PRICE_INPUT + output_tokens * _DEEPSEEK_PRICE_OUTPUT) / 1_000_000
        return round(cost, 6)

    def _log_call(
        self,
        provider: str,
        model: str,
        task_type: str,
        input_tokens: int,
        output_tokens: int,
        cost_cny: float,
        elapsed_ms: int,
    ) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO llm_calls
                   (ts, provider, model, task_type, input_tokens, output_tokens, cost_cny, elapsed_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), provider, model, task_type,
                 input_tokens, output_tokens, cost_cny, elapsed_ms),
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def chat(
        self,
        messages: list[dict],
        task_type: TaskType = "flash",
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str:
        """
        发送对话请求，返回 assistant 回复内容字符串。

        Args:
            messages: OpenAI 格式消息列表
            task_type: "flash" 用便宜模型，"pro" 用强模型
            temperature: 采样温度
            max_tokens: 最大输出 token 数
        """
        client, model = self._get_client(task_type)
        start = time.monotonic()

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        cost = self._estimate_cost(model, input_tokens, output_tokens)

        self._log_call(
            provider=self._settings.llm_provider,
            model=model,
            task_type=task_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=cost,
            elapsed_ms=elapsed_ms,
        )

        logger.debug(
            "LLM调用完成: model=%s input=%d output=%d cost=¥%.4f elapsed=%dms",
            model, input_tokens, output_tokens, cost, elapsed_ms,
        )

        return response.choices[0].message.content or ""

    def batch_chat(
        self,
        prompt_template: str,
        items: list[str],
        task_type: TaskType = "flash",
        batch_size: int = 20,
    ) -> list[str]:
        """
        批量调用：将 items 分批塞入 prompt，减少 API 请求次数。

        Args:
            prompt_template: 含 {items} 占位符的 prompt 模板
            items: 待处理的文本列表
            task_type: 使用的模型等级
            batch_size: 每批最多几条
        """
        results: list[str] = []
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            numbered = "\n".join(f"{j+1}. {item}" for j, item in enumerate(batch))
            prompt = prompt_template.format(items=numbered)
            messages = [{"role": "user", "content": prompt}]
            result = self.chat(messages, task_type=task_type)
            results.append(result)
        return results

    def get_daily_cost_summary(self, target_date: date | None = None) -> dict:
        """查询指定日期（默认今日）的累计 token 消耗与费用。"""
        target = (target_date or date.today()).isoformat()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_cny)
                   FROM llm_calls WHERE ts LIKE ?""",
                (f"{target}%",),
            ).fetchone()

        calls, inp, out, cost = row
        return {
            "date": target,
            "calls": calls or 0,
            "input_tokens": inp or 0,
            "output_tokens": out or 0,
            "estimated_cost_cny": round(cost or 0.0, 4),
        }
