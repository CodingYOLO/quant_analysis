"""
反向策略推荐（strategy scout）：只选一只票 → 自动用【全部信号库】回测它 →
按「样本量 + T+5 期望 + 盈亏比」打分排名，推荐最贴这只票股性的短线打法。

与正向回测（选票+选信号）相反：这里替用户把 16 个信号都跑一遍，回答
“这只票历史上更吃哪种打法”。

设计要点（复用现成、零造轮子）：
- 只 `load_kline` 一次（带 200 日缓冲），对每个信号复用 `sector_backtest._occurrences`
  找出现点（含防未来函数：买次日开盘 / 卖 T+N 收盘），再用 `_agg` 聚合。
  相比反复调 `backtest_stock_signal` 省去 16 倍取数，单进程友好。
- 打分以 **T+5 期望收益** 为核心，按样本量做**贝叶斯式收缩**（小样本自动向 0 降权），
  叠加盈亏比质量微调 → 既奖励高期望、又惩罚不可信的小样本。
- 股性（妖性/趋势性/波动）复用 `build_stock_profile`，做规则化“为什么适合这票”一句话。

诚实纪律（符合 CLAUDE.md 禁止项）：
- 这是**确定性统计排名**，不是 LLM 输出胜率排序；
- 必标样本量、不输出“必胜/概率”、小样本显式降权并告警；
- 窗口默认近 3 个月（2024-09 后市场风格剧变，远期数据失真），可调。
- LLM 仅在 `generate_scout_note` 做**解读润色**，不参与排名、不造新数字。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from app.backtest.sector_backtest import _occurrences
from app.backtest.signal_backtest import _agg, _signal_defs
from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.data.kline_loader import load_kline

logger = logging.getLogger(__name__)

# ── 评分/门槛常量（集中可调，避免硬编码散落）──────────────────────────────────
HORIZON = 5              # 主口径持有期 T+5（与回测引擎“胜负看 T+5”一致）
_SHRINK_K = 6            # 样本收缩系数：置信度 = n/(n+K)，n=6 时置信 0.5
_DEFAULT_MIN_SAMPLE = 4  # 入推荐榜的最低样本量（近 3 月窗口下兼顾“有结果”与“别太水”）
_AMPLE_SAMPLE = 8        # 足量阈值：低于此即便入榜也标注“样本偏少·仅参考”
_REC_TOP = 5             # 推荐数量上限
_PF_CAP = 3.0            # 盈亏比封顶（防极端值主导排序）
_HIST_BUFFER_DAYS = 200  # 指标预热：信号判定所需的窗口前历史
_PROFILE_LOOKBACK = 600  # 股性画像回看交易日（股性需长周期，独立于回测短窗口）

_DISCLAIMER = "以上为基于历史回测的策略适配统计，非涨跌预测、不构成投资建议；历史回测≠未来收益。"

# 信号分类（用于规则理由“与股性是否相符”的映射；未知键归“其他”）
_CATEGORY: dict[str, str] = {
    "big_yang_volume": "追涨突破", "breakout_high_20": "追涨突破",
    "platform_breakout": "追涨突破", "vol_price_surge": "追涨突破",
    "shrink_pullback_ma10": "低吸超跌", "shrink_pullback_ma20": "低吸超跌",
    "rsi_oversold_recover": "低吸超跌", "kdj_gold": "低吸超跌", "td_buy9": "低吸超跌",
    "ma5_cross_ma10": "趋势跟随", "ma_short_bull": "趋势跟随", "ma_bull_stack": "趋势跟随",
    "first_above_ma10": "趋势跟随", "ema_bull": "趋势跟随",
    "macd_gold": "经典金叉", "macd_gold_above_zero": "经典金叉",
}


# ──────────────────────────────────────────────
# 单信号评分结果
# ──────────────────────────────────────────────

@dataclass
class SignalScore:
    """单个信号在该票近窗口内的表现 + 评分。所有数字均来自真实回测聚合。"""
    key: str = ""
    label: str = ""
    category: str = ""
    n: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best: float = 0.0
    worst: float = 0.0
    conf: float = 0.0        # 样本置信度 0~1
    score: float = 0.0       # 排序分（收缩后期望 × 盈亏比质量）
    tier: str = "none"       # rec / rec_thin / neg / thin / none
    note: str = ""           # 人话标注


# ──────────────────────────────────────────────
# 打分（纯函数，可单测）
# ──────────────────────────────────────────────

def _score_signal(key: str, label: str, stat: dict, min_sample: int) -> SignalScore:
    """
    把单信号的 T+5 聚合统计 → 评分 + 分层。

    分数 = 期望收益 × 样本置信度 × 盈亏比质量。
    - 样本置信度 conf=n/(n+K)：小样本天然向 0 收缩（诚实降权）。
    - 盈亏比质量 ∈[0.6,1.0]：盈亏比越高（封顶3）略加分，奖励“稳”。
    - 期望为负 → 分数为负 → 不进推荐。
    """
    n = int(stat.get("n", 0))
    cat = _CATEGORY.get(key, "其他")
    base = SignalScore(key=key, label=label, category=cat, n=n,
                       win_rate=float(stat.get("win_rate", 0.0)),
                       avg_return=float(stat.get("avg_return", 0.0)),
                       profit_factor=float(stat.get("profit_factor", 0.0)),
                       avg_win=float(stat.get("avg_win", 0.0)),
                       avg_loss=float(stat.get("avg_loss", 0.0)),
                       best=float(stat.get("best", 0.0)),
                       worst=float(stat.get("worst", 0.0)))
    if n == 0:
        base.tier, base.note = "none", "本期未触发"
        return base

    conf = round(n / (n + _SHRINK_K), 3)
    pf_q = 0.6 + 0.4 * (min(base.profit_factor, _PF_CAP) / _PF_CAP if base.profit_factor > 0 else 0)
    base.conf = conf
    base.score = round(base.avg_return * conf * pf_q, 3)
    base.tier, base.note = _classify(n, base.avg_return, min_sample)
    return base


def _classify(n: int, expect: float, min_sample: int) -> tuple[str, str]:
    """根据样本量与期望判定推荐分层 + 人话标注。"""
    if n < min_sample:
        return "thin", f"样本极少(仅{n}次)·仅参考"
    if expect <= 0:
        return "neg", f"期望为负({expect:+}%)·不适配"
    if n < _AMPLE_SAMPLE:
        return "rec_thin", f"可参考·样本偏少(n={n}<{_AMPLE_SAMPLE})"
    return "rec", f"样本充足(n={n})"


def _rank(scores: list[SignalScore]) -> list[SignalScore]:
    """排序：先按 tier 优先级（推荐>偏少推荐>样本极少>负期望>未触发），同档按分数降序。"""
    order = {"rec": 0, "rec_thin": 1, "thin": 2, "neg": 3, "none": 4}
    return sorted(scores, key=lambda s: (order.get(s.tier, 9), -s.score, -s.n))


# ──────────────────────────────────────────────
# 股性 → 规则化“为什么适合这票”（纯函数）
# ──────────────────────────────────────────────

def _character(metrics: dict) -> tuple[str, set[str]]:
    """从股性 metrics 提炼一句话性格 + 标志集合（供匹配信号类别）。"""
    flags: set[str] = set()
    parts: list[str] = []
    if metrics.get("limit_up_1y", 0) >= 12 or metrics.get("max_board", 0) >= 3:
        flags.add("妖性"); parts.append("妖性强·游资活跃")
    if metrics.get("above_ma20_ratio", 0) >= 55:
        flags.add("趋势"); parts.append("趋势性强·适合持有")
    elif metrics.get("above_ma20_ratio", 100) < 45:
        flags.add("震荡"); parts.append("偏震荡·低吸高抛为主")
    v = metrics.get("volatility_annual", 0)
    if v >= 45:
        flags.add("高波动"); parts.append(f"高波动({v:.0f}%)·短线属性")
    elif v < 28:
        flags.add("低波动"); parts.append(f"低波动({v:.0f}%)·稳健")
    return ("、".join(parts) if parts else "股性中性", flags)


def _fit_phrase(category: str, flags: set[str]) -> str:
    """推荐信号类别 vs 股性标志 → 是否相符的人话。"""
    if category == "追涨突破" and ("妖性" in flags or "高波动" in flags):
        return "相符（妖性/高波动票更吃突破打板）"
    if category in ("趋势跟随", "经典金叉") and "趋势" in flags:
        return "相符（趋势票更吃均线跟随）"
    if category == "低吸超跌" and "震荡" in flags:
        return "相符（震荡票更吃低吸高抛）"
    return "可留意是否匹配当前股性"


def _rule_rationale(top: SignalScore | None, metrics: dict, window_label: str) -> str:
    """规则化一句话理由（确定性、零成本，作为 LLM 润色的兜底基底）。"""
    char, flags = _character(metrics or {})
    if top is None:
        return (f"这只票{char}。近{window_label}内各信号样本偏少，暂难给出稳健推荐；"
                f"建议把窗口拉长到半年/1年再看。历史≠未来。")
    fit = _fit_phrase(top.category, flags)
    return (f"这只票{char}。近{window_label}内历史期望最高的是「{top.label}」"
            f"（T+5 均收益 {top.avg_return:+}%、胜率 {round(top.win_rate * 100)}%、n={top.n}），"
            f"属「{top.category}」类，与其股性{fit}。样本为近{window_label}统计，历史≠未来。")


# ──────────────────────────────────────────────
# 主入口：扫描全部信号
# ──────────────────────────────────────────────

def scout_strategies(ts_code: str, start: str, end: str,
                     provider: CompositeProvider | None = None, *,
                     name: str = "", min_sample: int = _DEFAULT_MIN_SAMPLE) -> dict:
    """
    对一只票在 [start, end] 窗口扫描全部信号，输出策略适配排名。

    参数：
        ts_code: 完整代码（如 600519.SH）。
        start/end: 评估窗口（YYYYMMDD）。引擎自动多取 ~200 日历史预热指标。
        provider: 数据源（依赖注入，便于单测）。
        name: 股票名（用于股性画像的涨跌停口径）。
        min_sample: 入推荐榜最低样本量。
    返回：
        {ok, ts_code, name, start, end, bars, horizon, min_sample, window_label,
         ranked[...], recommended[keys], profile_tags, rationale, n_eligible, msg}
        失败返回 {ok: False, msg}。
    """
    provider = provider or CompositeProvider()
    k = _load_window_kline(ts_code, start, end, provider)
    if k is None or k.empty:
        return {"ok": False, "ts_code": ts_code, "msg": f"{ts_code} 历史数据不足"}

    scores = _score_all_signals(k, start, min_sample)
    ranked = _rank(scores)
    recommended = [s for s in ranked if s.tier in ("rec", "rec_thin")][:_REC_TOP]

    metrics, profile_tags = _load_profile(ts_code, name, provider)
    window_label = _window_label(start, end)
    top = recommended[0] if recommended else None
    rationale = _rule_rationale(top, metrics, window_label)

    return {
        "ok": True, "ts_code": ts_code, "name": name,
        "start": str(k["trade_date"].iloc[0]) if not k.empty else start,
        "end": str(k["trade_date"].iloc[-1]), "bars": int(len(k)),
        "horizon": HORIZON, "min_sample": min_sample, "ample_sample": _AMPLE_SAMPLE,
        "window_label": window_label,
        "ranked": [asdict(s) for s in ranked],
        "recommended": [s.key for s in recommended],
        "profile_tags": profile_tags,
        "rationale": rationale,
        "n_total": len(scores),
        "n_eligible": len(recommended),
        "disclaimer": _DISCLAIMER,
        "msg": "" if recommended else f"近{window_label}样本偏少，建议拉长窗口再看",
    }


def _load_window_kline(ts_code: str, start: str, end: str,
                       provider: CompositeProvider) -> pd.DataFrame | None:
    """加载带 200 日缓冲的前复权日 K（仅一次，供全部信号复用）。"""
    buf_start = (datetime.datetime.strptime(start, "%Y%m%d")
                 - datetime.timedelta(days=_HIST_BUFFER_DAYS)).strftime("%Y%m%d")
    try:
        k = load_kline(ts_code, buf_start, end, provider, adj="qfq")
    except Exception:
        logger.exception("[scout] load_kline 失败 %s", ts_code)
        return None
    return k if k is not None and not k.empty else None


def _score_all_signals(k: pd.DataFrame, start: str, min_sample: int) -> list[SignalScore]:
    """对每个信号跑出现点 → 聚合 T+5 → 打分。"""
    out: list[SignalScore] = []
    for key, sd in _signal_defs().items():
        rets = [o["rets"][HORIZON] for o in _occurrences(k, sd, start) if HORIZON in o["rets"]]
        stat = asdict(_agg(HORIZON, rets))
        out.append(_score_signal(key, sd["label"], stat, min_sample))
    return out


def _load_profile(ts_code: str, name: str,
                  provider: CompositeProvider) -> tuple[dict, list[dict]]:
    """复用股性画像取 metrics + tags（失败不阻断 scout，返回空）。"""
    try:
        from app.strategy.stock_profile import build_stock_profile
        prof = build_stock_profile(ts_code, name, provider, lookback_days=_PROFILE_LOOKBACK)
        if prof.get("ok"):
            return prof.get("metrics", {}), prof.get("tags", [])
    except Exception:
        logger.exception("[scout] 股性画像获取失败 %s", ts_code)
    return {}, []


def _window_label(start: str, end: str) -> str:
    """把起止日转成“近N月/N年”可读窗口名（用于理由文本）。"""
    try:
        days = (datetime.datetime.strptime(end, "%Y%m%d")
                - datetime.datetime.strptime(start, "%Y%m%d")).days
    except Exception:
        return "该区间"
    if days <= 40:
        return "1个月"
    if days <= 100:
        return "3个月"
    if days <= 200:
        return "半年"
    if days <= 400:
        return "1年"
    return f"{round(days / 365, 1)}年"


# ──────────────────────────────────────────────
# LLM 理由润色（解读层，不参与排名；client 可注入零网络单测）
# ──────────────────────────────────────────────

def _note_cache_dir() -> Path:
    d = get_settings().cache_dir / "scout_note"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_note_facts(result: dict) -> str:
    """把 scout 排名 + 股性压成数字密集事实块（喂给 LLM 解读）。"""
    name = result.get("name") or result.get("ts_code", "")
    lines = [f"标的：{name}（{result.get('ts_code', '')}）",
             f"窗口：近{result.get('window_label', '')}，共 {result.get('bars', 0)} 根K线",
             "股性：" + "、".join(t.get("text", "") for t in (result.get("profile_tags") or [])),
             "各信号 T+5 表现（买次日开盘/卖T+5收盘，胜负看T+5>0）："]
    for s in (result.get("ranked") or [])[:10]:
        if s.get("n", 0) <= 0:
            continue
        lines.append(
            f"  · {s['label']}（{s.get('category', '')}）：n={s['n']}、"
            f"胜率{round(s['win_rate'] * 100)}%、期望{s['avg_return']:+}%、"
            f"盈亏比{s['profit_factor']}、最好{s['best']:+}%/最差{s['worst']:+}%（{s.get('note', '')}）")
    lines.append("规则初判：" + result.get("rationale", ""))
    return "\n".join(lines)


def _build_note_prompt(facts: str) -> str:
    """LLM 一句话点评 prompt：红线照搬研判（只解读、不预测、不荐买卖、标样本）。"""
    return (
        "你是严谨的A股量化助手。下面是对一只票做的【反向策略扫描已算好结果】："
        "把多个技术信号在该票近窗口内的历史表现都跑了一遍。请用 2-3 句话解读"
        "“这只票历史上更吃哪类打法、为什么（结合它的股性）”，给用户一个有洞察的总评。\n\n"
        "严格红线（违反即失败）：\n"
        "1. 只能引用下方给出的数字，绝不臆造或推算任何新数字；\n"
        "2. 绝不输出新的“胜率/概率”，绝不预测涨跌或目标价，绝不给买入/卖出指令；\n"
        "3. 必须点明这是历史统计、且对样本偏少(n<8)的信号显式提示“仅供参考”；\n"
        "4. 把“信号表现”和“股性”串起来讲（如妖性强→更吃突破/打板；趋势票→更吃均线跟随）。\n"
        "5. 只输出点评正文本身，不要分点、不要前后缀、不要代码块。\n\n"
        f"数据：\n{facts}"
    )


def generate_scout_note(result: dict, client=None) -> dict:
    """
    对 scout 结果生成 LLM 一句话点评。result=scout_strategies 的返回。
    client 可注入（零网络单测）；按 facts 指纹缓存避免重复花费。
    """
    if not result or not result.get("ok"):
        return {"ok": False, "msg": "请先运行策略扫描"}

    facts = build_note_facts(result)
    key = hashlib.md5(facts.encode("utf-8")).hexdigest()[:16]
    cache = _note_cache_dir() / f"{key}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    if client is None:
        from app.llm.client import LLMClient
        client = LLMClient()
    raw = client.chat([{"role": "user", "content": _build_note_prompt(facts)}],
                      task_type="pro", max_tokens=1500, temperature=0.3)

    st = get_settings()
    model = st.claude_model if st.llm_provider == "claude" else st.deepseek_pro_model
    note = (raw or "").strip()
    out = {"ok": bool(note), "note": note, "model": model, "disclaimer": _DISCLAIMER}
    if note:  # 仅缓存有效输出，避免把空/异常缓存下来
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return out
