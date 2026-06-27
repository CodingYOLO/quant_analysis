"""全市场实时行情快照：线程安全的内存最新值存储。

全推后台线程持续 update()，分析层/页面 read()。一把锁保护 dict 与时间戳；
拷贝出参避免外部改动内部状态。陈旧判断（is_stale）用于触发新浪兜底。
"""

from __future__ import annotations

import threading
import time

import pandas as pd


class MarketSnapshot:
    """`{ts_code: 最新行情 dict}` 的线程安全快照。"""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._updated_at = 0.0

    def update(self, quote: dict) -> None:
        """写入/覆盖单只最新行情（无 ts_code 忽略）。"""
        code = quote.get("ts_code")
        if not code:
            return
        with self._lock:
            self._data[code] = quote
            self._updated_at = time.time()

    def update_many(self, quotes: list[dict]) -> None:
        """批量写入（一条报文可能含多只）。"""
        if not quotes:
            return
        with self._lock:
            for q in quotes:
                code = q.get("ts_code")
                if code:
                    self._data[code] = q
            self._updated_at = time.time()

    def get(self, ts_code: str) -> dict | None:
        """读单只最新行情（返回拷贝）。"""
        with self._lock:
            q = self._data.get(ts_code)
            return dict(q) if q else None

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    @property
    def updated_at(self) -> float:
        """最近一次写入的本地时间戳（epoch 秒）；从未写入为 0。"""
        return self._updated_at

    def is_stale(self, max_age_sec: float) -> bool:
        """超过 max_age 秒未更新（或从未更新）视为陈旧 → 触发新浪兜底。"""
        if not self._updated_at:
            return True
        return (time.time() - self._updated_at) > max_age_sec

    def to_df(self, ts_codes: list[str] | None = None) -> pd.DataFrame:
        """导出 DataFrame（默认全量）；列与 get_realtime_quote 对齐。"""
        with self._lock:
            if ts_codes is None:
                rows = list(self._data.values())
            else:
                rows = [self._data[c] for c in ts_codes if c in self._data]
        return pd.DataFrame(rows)
