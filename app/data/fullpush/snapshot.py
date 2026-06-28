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
        self._updated_at = 0.0          # 任意来源最近写入（数据是否"新"）
        self._fullpush_at = 0.0         # 全推最近写入（全推是否"活"·区别于新浪兜底）
        self._source = ""               # 当前数据来源："全推" / "新浪兜底"

    def update(self, quote: dict) -> None:
        """写入/覆盖单只最新行情（全推路径·无 ts_code 忽略）。"""
        code = quote.get("ts_code")
        if not code:
            return
        with self._lock:
            self._data[code] = quote
            self._updated_at = self._fullpush_at = time.time()
            self._source = "全推"

    def update_many(self, quotes: list[dict]) -> None:
        """批量写入（全推路径·一条报文可能含多只）。"""
        if not quotes:
            return
        with self._lock:
            for q in quotes:
                code = q.get("ts_code")
                if code:
                    self._data[code] = q
            self._updated_at = self._fullpush_at = time.time()
            self._source = "全推"

    def update_external(self, quotes: list[dict]) -> None:
        """新浪兜底写入：刷新数据但**不**算"全推活"（来源标新浪兜底）。"""
        if not quotes:
            return
        with self._lock:
            for q in quotes:
                code = q.get("ts_code")
                if code:
                    self._data[code] = q
            self._updated_at = time.time()
            self._source = "新浪兜底"

    def get(self, ts_code: str) -> dict | None:
        """读单只最新行情（返回拷贝）。"""
        with self._lock:
            q = self._data.get(ts_code)
            return dict(q) if q else None

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def prices(self) -> dict[str, float]:
        """{ts_code: 现价} 轻量快照（供急拉/涨速采样）。"""
        with self._lock:
            return {c: float(q.get("price") or 0.0) for c, q in self._data.items()}

    def net_amounts(self) -> dict[str, float]:
        """{ts_code: 当日累计主动净买(亿)} 轻量（不建DataFrame·供资金持续/脉冲采样）。"""
        with self._lock:
            out = {}
            for c, q in self._data.items():
                inner = float(q.get("inner") or 0.0)
                outer = float(q.get("outer") or 0.0)
                price = float(q.get("price") or 0.0)
                out[c] = (outer - inner) * price / 1e6
            return out

    @property
    def updated_at(self) -> float:
        """最近一次写入的本地时间戳（epoch 秒）；从未写入为 0。"""
        return self._updated_at

    @property
    def source(self) -> str:
        """当前数据来源（全推 / 新浪兜底 / 空）。"""
        return self._source

    def is_stale(self, max_age_sec: float) -> bool:
        """任意来源超过 max_age 秒未更新（或从未更新）视为陈旧。"""
        if not self._updated_at:
            return True
        return (time.time() - self._updated_at) > max_age_sec

    def fullpush_stale(self, max_age_sec: float) -> bool:
        """**全推**超过 max_age 秒未供数（或从未）→ 全推断流，触发新浪兜底/告警。"""
        if not self._fullpush_at:
            return True
        return (time.time() - self._fullpush_at) > max_age_sec

    def to_df(self, ts_codes: list[str] | None = None) -> pd.DataFrame:
        """导出 DataFrame（默认全量）；列与 get_realtime_quote 对齐。"""
        with self._lock:
            if ts_codes is None:
                rows = list(self._data.values())
            else:
                rows = [self._data[c] for c in ts_codes if c in self._data]
        return pd.DataFrame(rows)
