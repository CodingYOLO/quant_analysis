"""取数编排：适配器取值 → 对齐到交易日（严格 point-in-time）→ 落库 + 运行日志。

**回补与每日增量是同一段代码**（`run(start, end)`，单日增量即 start==end），
两套逻辑必然漂移，一套不会。

对齐规则（决定了回看模式是否真的 point-in-time）：
  · 某交易日 D 只能看到 `可见日 <= D` 的观测值，取其中最新的一条；
  · **可见日 = as_of + lag_days**（自然日）——月频尤其关键：1 月的 CPI 要 2 月中旬才发布，
    绝不能出现在 1 月的面板上，否则回看模式会给出当时根本不可能知道的结论；
  · 取到的观测值若 `as_of < D`，说明是沿用旧值 → `is_stale=已沿用会话数`；
    超过 `max_carry_days`（daily 按交易日会话·weekly/monthly 按自然日）→ 写 NULL。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import pandas as pd

from app.macro import store
from app.macro.adapters import ADAPTERS, Adapter, Point

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    run_id: str
    start: str
    end: str
    trade_days: int
    rows: int
    ok: list[str]
    failed: dict[str, str]
    logs: list[dict]

    @property
    def ok_count(self) -> int:
        return len(self.ok)


def trading_days(start: str, end: str) -> list[str]:
    """区间内的交易日（升序）。复用项目现有交易日历，不另建。"""
    from app.data.composite_provider import CompositeProvider
    cal = CompositeProvider().get_trade_cal(start, end)
    if cal is None or cal.empty:
        return []
    open_days = cal[cal["is_open"] == 1]["cal_date"].astype(str)
    return sorted(d for d in open_days if start <= d <= end)


def _shift_days(ymd: str, days: int) -> str:
    return (pd.Timestamp(ymd) + pd.Timedelta(days=days)).strftime("%Y%m%d")


def align_to_trading_days(points: list[Point], days: list[str], lag_days: int,
                          max_carry_days: int, freq: str = "daily",
                          ref_days: list[str] | None = None) -> list[dict]:
    """把某指标的观测点对齐到交易日序列，返回待写入的行。

    返回的每行含 value/as_of/source/is_stale；取不到则 value=None（如实写 NULL）。

    **is_stale 存的是"已沿用的交易日会话数"**（0=当日新值），不是 0/1 布尔——
    评分层要区分"外盘隔夜差 1 个会话（正常）"与"断 2 个会话以上（源坏了）"。

    结转超限判定按频率分两种单位（单位混用会出错，各自理由）：
    · daily → **交易日会话数**。用自然日的话周五值沿用到周一=3天，每个周一都会被误判
      超限；"断 N 天=源坏了"的本意是 N 个交易时段没更新。会话数在 `ref_days`
      （完整交易日历，可比 `days` 更早）上用二分统计——单日增量时 `days` 只有 1 天，
      没有参照日历就数不出会话。
    · weekly/monthly → **自然日**。发布周期是日历概念（下月中旬发布=约45自然日）。
    """
    import bisect
    ref = sorted(ref_days or days)
    obs = sorted({(p.as_of, p.value, p.source) for p in points})
    rows: list[dict] = []
    idx = 0
    cur: tuple[str, float, str] | None = None
    for d in days:
        # 推进到所有"在 d 当天已可见"的观测（可见日 = as_of + lag_days）
        while idx < len(obs) and _shift_days(obs[idx][0], lag_days) <= d:
            cur = obs[idx]
            idx += 1
        if cur is None:
            rows.append({"trade_date": d, "value": None, "as_of": None,
                         "source": None, "is_stale": 0})
            continue
        as_of, value, src = cur
        visible = _shift_days(as_of, lag_days)
        sessions = bisect.bisect_right(ref, d) - bisect.bisect_right(ref, visible)
        if freq == "daily":
            over = sessions > max_carry_days
        else:
            over = (pd.Timestamp(d) - pd.Timestamp(as_of)).days - lag_days > max_carry_days
        if over:
            # 陈旧超限：写 NULL 而不是无限期沿用（沿用会让面板显示一个早已失效的"当前值"）；
            # as_of/source 保留供排查"断供从哪天开始"
            rows.append({"trade_date": d, "value": None, "as_of": as_of,
                         "source": src, "is_stale": sessions})
            continue
        rows.append({"trade_date": d, "value": float(value), "as_of": as_of,
                     "source": src, "is_stale": sessions})
    return rows


def run(start: str, end: str, codes: set[str] | None = None,
        run_id: str | None = None) -> SyncResult:
    """取数并落库。`codes=None` 表示所有启用指标。"""
    store.init_db()
    run_id = run_id or f"sync_{pd.Timestamp.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    meta = {m["code"]: m for m in store.get_meta()}
    want = set(codes) & set(meta) if codes else set(meta)
    days = trading_days(start, end)
    if not days:
        raise ValueError(f"{start}~{end} 无交易日")

    # 参照交易日历：往前多取 120 自然日，保证单日增量时也能数出"已沿用几个会话"
    ref_days = trading_days(_shift_days(start, -120), end)

    ok: list[str] = []
    failed: dict[str, str] = {}
    logs: list[dict] = []
    all_rows: list[dict] = []

    for ad in ADAPTERS:
        mine = set(ad.codes) & want
        if not mine:
            continue
        # 取数窗口向前多取 lookback：单日增量时 T+1 发布的源(margin/美债)在当晚
        # 只有 T-1/T-非本日 的数据·不回看就取不到任何点→假"empty"告警。
        # 重型逐日适配器(margin_ratio 分母按日读行情)自设小 lookback 控制成本。
        fetch_start = _shift_days(start, -int(getattr(ad, "lookback_days", 75)))
        try:
            from app.macro.adapters.base import timed
            pts, ms = timed(lambda: ad.fetch(fetch_start, end))
        except Exception as e:                      # 取数失败 → 该适配器全部指标写 NULL
            logger.warning("[macro] 适配器 %s 取数失败: %s", ad.name, e, exc_info=True)
            for c in sorted(mine):
                failed[c] = f"{type(e).__name__}: {e}"
                logs.append({"run_id": run_id, "trade_date": end, "code": c,
                             "status": "error", "rows": 0, "elapsed_ms": 0,
                             "err_msg": str(e)[:300]})
                all_rows += [dict(r, code=c, source_run_id=run_id)
                             for r in align_to_trading_days([], days, 0, 0,
                                                            ref_days=ref_days)]
            continue

        by_code: dict[str, list[Point]] = {}
        for p in pts:
            by_code.setdefault(p.code, []).append(p)
        for c in sorted(mine):
            m = meta[c]
            got = by_code.get(c, [])
            rows = align_to_trading_days(got, days, int(m["lag_days"]),
                                         int(m["max_carry_days"]),
                                         freq=m["freq"], ref_days=ref_days)
            all_rows += [dict(r, code=c, source_run_id=run_id) for r in rows]
            n_val = sum(1 for r in rows if r["value"] is not None)
            if n_val:
                ok.append(c)
                status = "ok"
            else:
                failed[c] = "适配器返回但该指标无有效值"
                status = "empty"
            logs.append({"run_id": run_id, "trade_date": end, "code": c, "status": status,
                         "rows": n_val, "elapsed_ms": ms,
                         "err_msg": "" if n_val else "no valid points"})

    # 启用了却没有任何适配器覆盖的指标：明确记为 error，不能悄无声息地不存在
    from app.macro.adapters import covered_codes
    for c in sorted(want - covered_codes()):
        failed[c] = "无适配器覆盖（registry 启用了但 adapters 未登记）"
        logs.append({"run_id": run_id, "trade_date": end, "code": c, "status": "error",
                     "rows": 0, "elapsed_ms": 0, "err_msg": "no adapter registered"})

    if all_rows:
        store.upsert_daily(all_rows)
    store.log_runs(logs)
    return SyncResult(run_id=run_id, start=start, end=end, trade_days=len(days),
                      rows=len(all_rows), ok=sorted(ok), failed=failed, logs=logs)
