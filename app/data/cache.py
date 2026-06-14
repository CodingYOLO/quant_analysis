"""
数据缓存、限频、重试封装。
同一交易日同一接口只拉一次，结果存 parquet；
失败时指数退避重试；接口调用间强制 sleep。
"""

import functools
import logging
import time
from pathlib import Path
from typing import Callable, Any

import pandas as pd
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

# akshare 调用最小间隔（秒）
_RATE_LIMIT_SECONDS = 1.5
_last_call_time: dict[str, float] = {}


def _rate_limit(key: str) -> None:
    """对指定 key 做限频，距上次调用不足间隔则 sleep。"""
    now = time.monotonic()
    last = _last_call_time.get(key, 0.0)
    gap = _RATE_LIMIT_SECONDS - (now - last)
    if gap > 0:
        time.sleep(gap)
    _last_call_time[key] = time.monotonic()


def _cache_path(name: str, date_key: str) -> Path:
    """返回缓存文件路径: data_cache/<name>/<date_key>.parquet"""
    settings = get_settings()
    p = settings.cache_dir / name
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{date_key}.parquet"


def cached_daily(name: str, date_key: str, fetch_fn: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    """
    按 (name, date_key) 读取缓存；未命中则调用 fetch_fn 拉取并写入缓存。

    Args:
        name: 接口标识符，如 "tushare_daily"
        date_key: 缓存键，通常是交易日 YYYYMMDD
        fetch_fn: 无参数的拉取函数
    """
    path = _cache_path(name, date_key)
    if path.exists():
        logger.debug("缓存命中: %s / %s", name, date_key)
        return pd.read_parquet(path)

    logger.info("缓存未命中，拉取: %s / %s", name, date_key)
    df = fetch_fn()
    if df is not None and not df.empty:
        df.to_parquet(path, index=False)
    return df


def with_retry(
    stop_attempts: int = 3,
    wait_min: float = 2.0,
    wait_max: float = 30.0,
) -> Callable:
    """返回一个带指数退避重试的装饰器。"""
    return retry(
        stop=stop_after_attempt(stop_attempts),
        wait=wait_exponential(multiplier=1, min=wait_min, max=wait_max),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


def rate_limited_call(key: str, fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """限频后调用 fn，key 用于独立限频（不同接口互不干扰）。"""
    _rate_limit(key)
    return fn(*args, **kwargs)
