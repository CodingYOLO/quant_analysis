"""
信号回测：量化验证「吴川体系新增信号」的真实前向收益。

回答的实战问题（纯统计，非 LLM 排序，符合 CLAUDE.md 禁止项）：
  1. 龙虎榜按主导资金类型（游资/机构/散户/北向/量化）上榜后，
     次日溢价与 T+1/T+3/T+5 收益分布如何？
  2. 知名游资席位（章盟主/赵老哥/炒股养家…）上榜是否真有次日溢价优势？
  3. 全市场炸板率分档（情绪退潮强弱）后，次日大盘表现如何？

交易约定（与 forward_tracker 一致）：
  - 信号在交易日 d 收盘后确认（龙虎榜/炸板率均盘后公布）
  - 买入价 = 次交易日 T+1 开盘价（真实可执行）
  - 收益窗口：T+1/T+3/T+5 收盘相对 T+1 开盘
  - 次日溢价(gap) = T+1 开盘相对 d 收盘的跳空（游资打板最关心的指标）

数据：龙虎榜/炸板率走 market_extras（Tushare 官方），日线走 CompositeProvider。
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime

import pandas as pd

from app.data.composite_provider import CompositeProvider
from app.strategy import market_extras

logger = logging.getLogger(__name__)

# 前向持仓窗口（交易日，相对信号日 d）
_HORIZONS = (1, 3, 5)


# ──────────────────────────────────────────────
# 基础工具：交易日列表 + 日线缓存 + 前向收益
# ──────────────────────────────────────────────

def _trade_dates(provider: CompositeProvider, start: str, end: str) -> list[str]:
    """返回 [start, end] 区间内的交易日（升序）。"""
    cal = provider.get_trade_cal(start, end)
    return sorted(cal[cal["is_open"] == 1]["cal_date"].tolist())


def _dates_with_forward(provider: CompositeProvider, start: str, end: str) -> list[str]:
    """
    返回交易日列表，并向 end 之后多延伸约 12 个自然日，
    使临近 end 的信号日也能取到 T+3/T+5 的未来日线（若已收盘入库）。
    """
    from datetime import timedelta
    ext_end = (datetime.strptime(end, "%Y%m%d") + timedelta(days=12)).strftime("%Y%m%d")
    return _trade_dates(provider, start, ext_end)


def _make_daily_loader(provider: CompositeProvider):
    """返回一个按交易日缓存的日线取数闭包（ts_code 为索引）。"""
    cache: dict[str, pd.DataFrame | None] = {}

    def get_daily(date: str) -> pd.DataFrame | None:
        if date not in cache:
            try:
                df = provider.get_daily(date)
                cache[date] = df.set_index("ts_code") if df is not None and not df.empty else None
            except Exception:
                cache[date] = None
        return cache[date]

    return get_daily


def _forward_returns(code: str, d_idx: int, dates: list[str], get_daily) -> dict | None:
    """
    计算单只股票在信号日 d 之后的前向收益。

    Returns:
        dict(gap, t1, t3, t5)（%），数据不足返回 None。
        买入价=T+1 开盘；t{N}=T+N 收盘相对 T+1 开盘；gap=T+1 开盘相对 d 收盘跳空。
    """
    if d_idx + 1 >= len(dates):
        return None
    d_df = get_daily(dates[d_idx])
    t1_df = get_daily(dates[d_idx + 1])
    if d_df is None or t1_df is None or code not in d_df.index or code not in t1_df.index:
        return None

    d_close = float(d_df.loc[code, "close"])
    entry = float(t1_df.loc[code, "open"])
    if d_close <= 0 or entry <= 0:
        return None

    out = {"gap": round((entry - d_close) / d_close * 100, 3)}
    for h in _HORIZONS:
        j = d_idx + h
        if j >= len(dates):
            out[f"t{h}"] = None
            continue
        df_h = get_daily(dates[j])
        if df_h is None or code not in df_h.index:
            out[f"t{h}"] = None
            continue
        exit_close = float(df_h.loc[code, "close"])
        out[f"t{h}"] = round((exit_close - entry) / entry * 100, 3) if exit_close > 0 else None
    return out


def _agg(values: list[float]) -> dict:
    """对一组收益率求 样本数/胜率/均值/中位数。空列表返回零值。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "win_rate": None, "avg": None, "median": None}
    wins = sum(1 for v in vals if v > 0)
    return {
        "n": len(vals),
        "win_rate": round(wins / len(vals) * 100, 1),
        "avg": round(statistics.mean(vals), 2),
        "median": round(statistics.median(vals), 2),
    }


# ──────────────────────────────────────────────
# 信号 1+2：龙虎榜主导类型 / 知名游资席位
# ──────────────────────────────────────────────

def evaluate_lhb_signals(start: str, end: str) -> dict:
    """
    回测龙虎榜上榜个股按「主导资金类型」与「知名游资席位」的前向收益分布。

    Returns:
        {
          "sample_days": int,
          "by_dominant": {类型: {gap:{...}, t1:{...}, t3:{...}, t5:{...}, count:int}},
          "by_famous":   {游资昵称: {同上}},
        }
    """
    provider = CompositeProvider()
    dates = _dates_with_forward(provider, start, end)
    get_daily = _make_daily_loader(provider)

    # 收集每个桶的逐笔收益序列
    dom_buckets: dict[str, dict[str, list]] = {}
    fam_buckets: dict[str, dict[str, list]] = {}

    def _push(buckets: dict, key: str, ret: dict) -> None:
        b = buckets.setdefault(key, {"gap": [], "t1": [], "t3": [], "t5": []})
        b["gap"].append(ret["gap"])
        for h in _HORIZONS:
            b[f"t{h}"].append(ret[f"t{h}"])

    sample_days = 0
    for d_idx, d in enumerate(dates):
        if d > end:
            break  # 只对 end 及之前的交易日产生信号（之后仅作前向取价）
        if d_idx + 1 >= len(dates):
            break  # 无次日，无法计算买入价
        lhb = market_extras.get_dragon_tiger(d, provider)
        if not lhb:
            continue
        sample_days += 1
        for code, info in lhb.items():
            ret = _forward_returns(code, d_idx, dates, get_daily)
            if ret is None:
                continue
            _push(dom_buckets, info.get("dominant", "营业部"), ret)
            # 知名游资席位（一只票可能含多个，逐一计入）
            for seat in info.get("seats", []):
                tag = seat.get("tag", "")
                if "游资·" in tag:
                    nick = tag.replace("🔥游资·", "")
                    _push(fam_buckets, nick, ret)

    return {
        "sample_days": sample_days,
        "by_dominant": _summarize_buckets(dom_buckets),
        "by_famous": _summarize_buckets(fam_buckets),
    }


def _summarize_buckets(buckets: dict[str, dict[str, list]]) -> dict:
    """把逐笔收益序列桶聚合成统计指标，按 t1 样本数降序。"""
    out = {}
    for key, series in buckets.items():
        out[key] = {
            "count": len(series["t1"]),
            "gap": _agg(series["gap"]),
            **{f"t{h}": _agg(series[f"t{h}"]) for h in _HORIZONS},
        }
    return dict(sorted(out.items(), key=lambda kv: kv[1]["count"], reverse=True))


# ──────────────────────────────────────────────
# 信号 3：炸板率分档 → 次日大盘表现
# ──────────────────────────────────────────────

# 炸板率分档（%）：低=情绪强，高=情绪退潮
_ZB_BANDS = [(0, 15, "低(<15%)强势"), (15, 30, "中(15-30%)"), (30, 100, "高(≥30%)退潮")]


def evaluate_zhaban_signal(start: str, end: str) -> dict:
    """
    回测全市场「炸板率」分档后的次日大盘表现（全市场个股次日涨跌中位数为大盘代理）。

    Returns:
        {"sample_days": int, "by_band": {档位: {n, next_day_median_avg, up_day_rate}}}
    """
    provider = CompositeProvider()
    dates = _dates_with_forward(provider, start, end)
    get_daily = _make_daily_loader(provider)

    band_rows: dict[str, list[float]] = {label: [] for *_, label in _ZB_BANDS}

    for d_idx, d in enumerate(dates):
        if d > end:
            break
        if d_idx + 1 >= len(dates):
            break
        info = market_extras.get_limit_analysis(d, provider)
        zb_rate = info.get("zhaban_rate")
        if zb_rate is None:
            continue
        band = _band_label(zb_rate)
        # 次日全市场涨跌中位数（剔除极端新股 >21%）
        nd = get_daily(dates[d_idx + 1])
        if nd is None:
            continue
        pct = pd.to_numeric(nd["pct_chg"], errors="coerce")
        pct = pct[(pct.notna()) & (pct.abs() <= 21)]
        if pct.empty:
            continue
        band_rows[band].append(float(pct.median()))

    by_band = {}
    for *_, label in _ZB_BANDS:
        vals = band_rows[label]
        if vals:
            up_days = sum(1 for v in vals if v > 0)
            by_band[label] = {
                "n": len(vals),
                "next_day_median_avg": round(statistics.mean(vals), 3),
                "up_day_rate": round(up_days / len(vals) * 100, 1),
            }
        else:
            by_band[label] = {"n": 0, "next_day_median_avg": None, "up_day_rate": None}

    return {"sample_days": sum(len(v) for v in band_rows.values()), "by_band": by_band}


def _band_label(zb_rate: float) -> str:
    """按炸板率落入分档标签。"""
    for lo, hi, label in _ZB_BANDS:
        if lo <= zb_rate < hi:
            return label
    return _ZB_BANDS[-1][2]


# ──────────────────────────────────────────────
# 报告生成
# ──────────────────────────────────────────────

def build_signal_report(start: str, end: str) -> str:
    """
    跑完整信号回测并生成带时间戳的 Markdown 报告，返回文件路径。
    """
    from app.config import get_settings

    logger.info("[信号回测] %s ~ %s 龙虎榜信号评估中…", start, end)
    lhb = evaluate_lhb_signals(start, end)
    logger.info("[信号回测] 炸板率信号评估中…")
    zb = evaluate_zhaban_signal(start, end)

    md = _render_report(start, end, lhb, zb)

    settings = get_settings()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = settings.report_dir / f"signal_eval_{start}_{end}_{ts}.md"
    path.write_text(md, encoding="utf-8")
    logger.info("[信号回测] 报告已保存: %s", path)
    return str(path)


def _render_report(start: str, end: str, lhb: dict, zb: dict) -> str:
    """渲染 Markdown 报告。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# 信号回测报告（龙虎榜 / 炸板率）",
        f"> 📅 生成时间 **{now}**　|　回测区间 **{start} ~ {end}**",
        "> 买入价=次日(T+1)开盘；T+N=T+N 收盘相对买入价；gap=次日竞价跳空。纯量化统计，不构成投资建议。",
        "",
        "## 一、龙虎榜按主导资金类型",
        f"> 覆盖 {lhb['sample_days']} 个有龙虎榜的交易日。",
        "",
    ]
    lines += _bucket_table(lhb["by_dominant"], first_col="主导类型")

    lines += ["", "## 二、知名游资席位上榜表现", ""]
    if lhb["by_famous"]:
        lines += _bucket_table(lhb["by_famous"], first_col="游资席位")
    else:
        lines.append("（区间内无识别到的知名游资席位上榜）")

    lines += ["", "## 三、全市场炸板率分档 → 次日大盘", f"> 覆盖 {zb['sample_days']} 个交易日。次日大盘=全市场个股涨跌中位数。", ""]
    lines += [
        "| 炸板率档位 | 样本天数 | 次日大盘中位数均值 | 次日上涨概率 |",
        "|---|---|---|---|",
    ]
    for label, s in zb["by_band"].items():
        if s["n"]:
            lines.append(f"| {label} | {s['n']} | {s['next_day_median_avg']:+.2f}% | {s['up_day_rate']:.0f}% |")
        else:
            lines.append(f"| {label} | 0 | — | — |")

    lines += [
        "",
        "---",
        "> ⚠️ 解读提示：胜率/收益为历史统计，受区间与样本量影响，"
        "样本<30 的桶仅供参考；游资风格会随市场切换，不代表未来。",
    ]
    return "\n".join(lines)


def _bucket_table(buckets: dict, first_col: str) -> list[str]:
    """把桶统计渲染成表格行。"""
    rows = [
        f"| {first_col} | 样本 | 次日gap | T+1胜率 | T+1均值 | T+3胜率 | T+3均值 | T+5胜率 | T+5均值 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    def fmt(stat: dict, kind: str) -> str:
        if stat["n"] == 0 or stat.get(kind) is None:
            return "—"
        val = stat[kind]
        return f"{val:+.2f}%" if kind in ("avg", "median") else f"{val:.0f}%"

    for key, s in buckets.items():
        rows.append(
            f"| {key} | {s['count']} "
            f"| {fmt(s['gap'], 'avg')} "
            f"| {fmt(s['t1'], 'win_rate')} | {fmt(s['t1'], 'avg')} "
            f"| {fmt(s['t3'], 'win_rate')} | {fmt(s['t3'], 'avg')} "
            f"| {fmt(s['t5'], 'win_rate')} | {fmt(s['t5'], 'avg')} |"
        )
    return rows
