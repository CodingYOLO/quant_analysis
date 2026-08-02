"""取数适配器基类与通用守卫。

设计原则：**回补与每日增量走同一段代码**——适配器统一接收 `(start, end)` 区间、返回
`list[Point]`，单日增量只是 `start == end` 的特例。两套逻辑会漂移，一套不会。

三个守卫都是被真实事故倒逼出来的，**所有适配器强制使用**：
  `require_date`         —— 缓存 parquet 的数据日期 ≠ 目标交易日（上游未更新时接口会回上一日数据）
  `require_all_sources`  —— 多源汇总时任一源缺失（裸 sum 会跳过 NaN，静默产出假暴跌）
  `guard_truncation`     —— 接口静默截断（返回行数恰好等于上限）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import pandas as pd

logger = logging.getLogger(__name__)


class MacroFetchError(RuntimeError):
    """取数失败的基类。上层一律：写 NULL + 落 run_log + 告警，**绝不用旧值静默顶替**。"""


class StaleDataError(MacroFetchError):
    """数据日期 ≠ 目标交易日（上游尚未更新）。"""


class MissingSourceError(MacroFetchError):
    """多源汇总时有源缺失，不可部分求和。"""


class TruncatedError(MacroFetchError):
    """返回行数撞上接口静默截断上限。"""


@dataclass(frozen=True)
class Point:
    """一个指标在某个**数据自身日期**上的取值。

    `as_of` 是数据的真实时点（不是写入时间，也不是交易日）——对齐到交易日由 `sync` 负责。
    月频指标的 `as_of` 用报告期月末，配合 `metric_meta.lag_days` 决定"哪天起才可见"，
    这是 point-in-time 正确性的关键：不能让 1 月的 CPI 出现在 1 月的面板上。
    """
    code: str
    as_of: str          # YYYYMMDD
    value: float
    source: str         # 实际取到该值的源，落 macro_daily.source


class Adapter(Protocol):
    """适配器协议。新增指标 = 写一个适配器 + 在 registry 登记，不改计算层与前端。"""

    name: str
    codes: tuple[str, ...]

    def fetch(self, start: str, end: str) -> list[Point]:
        ...


# ──────────────────────────────────────────────
# 守卫
# ──────────────────────────────────────────────

def require_date(df: pd.DataFrame | None, expect: str, api: str,
                 date_col: str = "trade_date") -> pd.DataFrame:
    """校验返回数据的日期 == 目标交易日；不等即抛。

    为什么必须校验：上游未更新时接口可能**返回上一交易日的数据**，而缓存文件名却是今天——
    直接采用就等于"拿昨天的数据冒充今天"。实测 `get_daily`/`get_money_flow` 均带 `trade_date` 列
    且唯一值恰为目标日，校验成本几乎为零。
    """
    if df is None or df.empty:
        raise StaleDataError(f"{api}: {expect} 无数据返回")
    if date_col not in df.columns:
        raise StaleDataError(f"{api}: 缺 {date_col} 列，无法校验数据日期")
    got = set(df[date_col].astype(str).unique())
    if got != {expect}:
        raise StaleDataError(f"{api}: 数据日期 {sorted(got)[:3]} ≠ 目标交易日 {expect}")
    return df


def require_all_sources(parts: Mapping[str, Any], api: str) -> None:
    """多源汇总前校验**每一个源都真的返回了**，缺任一即抛，绝不部分求和。

    ⚠️实测事故：20260731 融资余额只有 SSE 有数据、SZSE/BSE 未发布，
    `sum()` 跳过 NaN 后得 13274 亿（实际约 25845 亿），静默产出 -49% 假暴跌。
    同一失效模式也存在于：涨跌家数跨板块、ETF 份额跨基金、成交额跨市场。

    **判据只看"有没有返回"，不看数值大小**——北交所两融余额本来就只有几十亿，
    用"值过小"当缺失判据必然误杀真实小值。
    """
    missing = [k for k, v in parts.items()
               if v is None or (isinstance(v, (pd.DataFrame, pd.Series)) and v.empty)]
    if missing:
        raise MissingSourceError(f"{api}: 源缺失 {missing}（共需 {list(parts)}），拒绝部分汇总")


def guard_truncation(rows: int, cap: int, api: str) -> None:
    """行数恰好等于上限 → 判定为静默截断。

    ⚠️踩过两次：`fina_indicator_vip` 单次上限 12000 行；`moneyflow_hsgt` 单次上限 300 行。
    两者都不报错、只是少给数据，不校验就会把"接口截断"误判成"这些日子没有数据"。
    """
    if cap and rows >= cap:
        raise TruncatedError(f"{api}: 返回 {rows} 行已达上限 {cap}，必须分页/分片取数")


def paged_fetch(fn: Callable[[int], pd.DataFrame | None], page: int,
                max_pages: int = 200) -> pd.DataFrame:
    """按 offset 分页取满。`fn(offset)` 返回一页；不足一页即结束。"""
    parts: list[pd.DataFrame] = []
    offset = 0
    for _ in range(max_pages):
        chunk = fn(offset)
        if chunk is None or chunk.empty:
            break
        parts.append(chunk)
        offset += len(chunk)
        if len(chunk) < page:
            break
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def sliced_fetch(fn: Callable[[str, str], pd.DataFrame | None],
                 start: str, end: str, months: int) -> pd.DataFrame:
    """按 N 个月切片取数（用于有区间长度上限的接口）。任一片失败只跳过该片并告警。

    ⚠️`repo_rate_hist` 实测：1/3/7/12 个月 OK，19 个月即报 `KeyError: frValueMap`；
    逐月排查过 2025 全年无坏月份 → 纯粹是区间长度限制。6 个月切片实测 6 片全成功。
    """
    parts: list[pd.DataFrame] = []
    cur = pd.Timestamp(start)
    stop = pd.Timestamp(end)
    while cur <= stop:
        seg_end = min(cur + pd.DateOffset(months=months) - pd.Timedelta(days=1), stop)
        try:
            chunk = fn(cur.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d"))
            if chunk is not None and not chunk.empty:
                parts.append(chunk)
        except Exception as e:
            logger.warning("[macro] 分片取数失败 %s~%s: %s",
                           cur.date(), seg_end.date(), e)
        cur = seg_end + pd.Timedelta(days=1)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# ──────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────

def to_ymd(v: Any) -> str:
    """各源日期形态各异（date / Timestamp / '2026-07-31' / '20260731'）→ 统一 YYYYMMDD。"""
    if isinstance(v, str):
        s = v.replace("-", "").replace("/", "").strip()
        return s[:8]
    return pd.Timestamp(v).strftime("%Y%m%d")


def month_end(month: str) -> str:
    """'202601' → '20260131'（月频指标的 as_of 用报告期月末）。"""
    m = str(month).replace("-", "")[:6]
    return (pd.Timestamp(f"{m[:4]}-{m[4:6]}-01") + pd.offsets.MonthEnd(0)).strftime("%Y%m%d")


def points_from_frame(df: pd.DataFrame, date_col: str, mapping: Mapping[str, str],
                      source: str, scale: Mapping[str, float] | None = None) -> list[Point]:
    """把宽表按 {列名: code} 展开成 Point 列表，跳过 NaN（**不填 0**）。"""
    out: list[Point] = []
    scale = scale or {}
    if df is None or df.empty or date_col not in df.columns:
        return out
    for col, code in mapping.items():
        if col not in df.columns:
            logger.warning("[macro] %s 缺列 %s → 指标 %s 本次无数据", source, col, code)
            continue
        vals = pd.to_numeric(df[col], errors="coerce")
        k = scale.get(code, 1.0)
        for d, v in zip(df[date_col], vals):
            if pd.isna(v):
                continue
            out.append(Point(code=code, as_of=to_ymd(d), value=float(v) * k, source=source))
    return out


def timed(fn: Callable[[], Any]) -> tuple[Any, int]:
    """执行并返回 (结果, 耗时毫秒)——run_log 要记每个指标的耗时。"""
    t0 = time.monotonic()
    r = fn()
    return r, int((time.monotonic() - t0) * 1000)
