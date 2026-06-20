"""
反向策略推荐 strategy_scout 单测：打分/分层/收缩/排序/规则理由 + 整库扫描 + LLM注入缓存。

零依赖（纯函数 + 假 Provider/假 client），可直接运行：python -m tests.test_strategy_scout
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

import app.backtest.strategy_scout as S


# ── 打分 + 分层（纯函数）─────────────────────────────────────────────────────
def test_score_and_tier() -> None:
    rec = S._score_signal("ma_bull_stack", "均线多头", {
        "n": 20, "win_rate": 0.6, "avg_return": 2.0, "profit_factor": 2.0,
        "avg_win": 3.0, "avg_loss": -1.5, "best": 8.0, "worst": -4.0}, min_sample=4)
    assert rec.tier == "rec" and rec.score > 0 and 0 < rec.conf < 1
    assert rec.category == "趋势跟随"          # 分类映射正确

    thin = S._score_signal("td_buy9", "TD九转", {"n": 3, "win_rate": 0.7,
                           "avg_return": 5.0, "profit_factor": 3.0}, min_sample=4)
    assert thin.tier == "thin" and "仅3次" in thin.note   # 样本极少·不入推荐

    neg = S._score_signal("kdj_gold", "KDJ金叉", {"n": 12, "win_rate": 0.3,
                          "avg_return": -1.2, "profit_factor": 0.6}, min_sample=4)
    assert neg.tier == "neg" and neg.score < 0            # 期望为负·不适配

    thin2 = S._score_signal("ema_bull", "EMA多头", {"n": 6, "win_rate": 0.55,
                            "avg_return": 1.0, "profit_factor": 1.4}, min_sample=4)
    assert thin2.tier == "rec_thin"                       # 入推荐但样本偏少(4≤n<8)

    none = S._score_signal("macd_gold", "MACD金叉", {"n": 0}, min_sample=4)
    assert none.tier == "none" and none.score == 0.0 and "未触发" in none.note


def test_small_sample_shrinkage() -> None:
    """同样期望/盈亏比，样本大的得分更高（小样本被诚实降权）。"""
    stat = lambda n: {"n": n, "win_rate": 0.6, "avg_return": 2.0, "profit_factor": 2.0}
    big = S._score_signal("ma_bull_stack", "x", stat(40), min_sample=4)
    small = S._score_signal("ma_bull_stack", "x", stat(4), min_sample=4)
    assert big.score > small.score and big.conf > small.conf


def test_rank_order() -> None:
    mk = lambda tier, score, n=10: S.SignalScore(key=tier, tier=tier, score=score, n=n)
    ranked = S._rank([mk("neg", -1), mk("rec", 0.5), mk("rec", 1.5),
                      mk("none", 0), mk("thin", 3.0), mk("rec_thin", 0.8)])
    tiers = [s.tier for s in ranked]
    assert tiers == ["rec", "rec", "rec_thin", "thin", "neg", "none"]   # 分层优先
    assert ranked[0].score == 1.5 and ranked[1].score == 0.5           # 同档按分降序


# ── 规则理由（纯函数）───────────────────────────────────────────────────────
def test_character_and_fit() -> None:
    spec_char, spec_flags = S._character({"limit_up_1y": 15, "max_board": 4,
                                          "above_ma20_ratio": 40, "volatility_annual": 55})
    assert "妖性" in spec_flags and "高波动" in spec_flags and "震荡" in spec_flags
    assert S._fit_phrase("追涨突破", spec_flags).startswith("相符")     # 妖股吃突破

    trend_char, trend_flags = S._character({"above_ma20_ratio": 70, "volatility_annual": 20})
    assert "趋势" in trend_flags and "低波动" in trend_flags
    assert S._fit_phrase("趋势跟随", trend_flags).startswith("相符")
    assert S._fit_phrase("低吸超跌", trend_flags) == "可留意是否匹配当前股性"


def test_rule_rationale_and_window_label() -> None:
    top = S.SignalScore(key="breakout_high_20", label="放量突破20日新高",
                        category="追涨突破", n=12, win_rate=0.66, avg_return=2.5)
    txt = S._rule_rationale(top, {"limit_up_1y": 14, "above_ma20_ratio": 40,
                                  "volatility_annual": 50}, "3个月")
    assert "放量突破20日新高" in txt and "n=12" in txt and "历史≠未来" in txt
    # 无推荐时给出“拉长窗口”建议，不报错
    assert "拉长" in S._rule_rationale(None, {}, "1个月")
    assert S._window_label("20260101", "20260331") == "3个月"
    assert S._window_label("20250101", "20260101") == "1年"


# ── 整库扫描（假 Provider，验证真实回测口径接通）─────────────────────────────
class _FakeProvider:
    """持续上涨序列：趋势/均线类信号恒成立、全胜 → 趋势跟随类应被推荐。"""
    def __init__(self, n: int = 160):
        dates = []
        for i in range(1, n + 1):     # 跨 2026 年 1~6 月，足够 3 月窗口取样
            mo, day = divmod(i - 1, 28)
            dates.append(f"2026{mo + 1:02d}{day + 1:02d}")
        self._df = pd.DataFrame({
            "trade_date": dates,
            "open": [100 + i * 0.5 for i in range(n)],
            "high": [101 + i * 0.5 for i in range(n)],
            "low": [99 + i * 0.5 for i in range(n)],
            "close": [100.5 + i * 0.5 for i in range(n)],
            "vol": [1000 + (i % 5) * 200 for i in range(n)],
            "amount": [1e5] * n, "pct_chg": [0.5] * n,
        })

    def get_stock_daily(self, ts_code, start, end):
        return self._df

    def get_adj_factor_series(self, ts_code, start, end):
        return pd.DataFrame({"trade_date": self._df["trade_date"],
                             "adj_factor": [1.0] * len(self._df)})

    def get_cyq_perf(self, ts_code, start, end):
        return pd.DataFrame()         # 无筹码 → 不影响 scout


def test_scout_strategies_integration() -> None:
    r = S.scout_strategies("TEST.SZ", "20260401", "20260620",
                           provider=_FakeProvider(), name="测试股")
    assert r["ok"] and r["n_total"] == 16 and r["horizon"] == 5
    assert r["recommended"]                          # 上涨序列必有推荐
    assert all(k in r for k in ("ranked", "rationale", "profile_tags", "disclaimer"))
    top = r["ranked"][0]
    assert top["n"] > 0 and top["avg_return"] > 0 and top["tier"] in ("rec", "rec_thin")
    assert {"key", "label", "category", "win_rate", "score", "note"} <= set(top.keys())
    assert "历史≠未来" in r["rationale"]              # 诚实免责常驻


def test_scout_bad_code() -> None:
    class _Empty(_FakeProvider):
        def get_stock_daily(self, *a):
            return pd.DataFrame()
    out = S.scout_strategies("NONE.SZ", "20260401", "20260620", provider=_Empty())
    assert out["ok"] is False and "不足" in out["msg"]


# ── LLM 解读润色（注入 fake client，零网络 + 缓存）──────────────────────────
class _FakeLLM:
    def __init__(self, reply: str):
        self.reply, self.calls = reply, 0

    def chat(self, messages, task_type="flash", **kw):
        self.calls += 1
        return self.reply


def _scout_result() -> dict:
    return {
        "ok": True, "ts_code": "600519.SH", "name": "贵州茅台", "bars": 60,
        "window_label": "3个月", "rationale": "这只票趋势性强。",
        "profile_tags": [{"text": "趋势性强"}, {"text": "低波动"}],
        "ranked": [
            {"key": "ma_bull_stack", "label": "均线多头排列", "category": "趋势跟随",
             "n": 18, "win_rate": 0.61, "avg_return": 1.8, "profit_factor": 1.6,
             "best": 6.0, "worst": -3.0, "note": "样本充足(n=18)"},
            {"key": "td_buy9", "label": "TD九转", "category": "低吸超跌",
             "n": 0, "win_rate": 0.0, "avg_return": 0.0, "profit_factor": 0.0,
             "best": 0.0, "worst": 0.0, "note": "本期未触发"},
        ],
    }


def test_build_note_facts() -> None:
    f = S.build_note_facts(_scout_result())
    assert "贵州茅台" in f and "近3个月" in f and "趋势性强" in f
    assert "均线多头排列" in f and "n=18" in f and "期望+1.8%" in f
    assert "TD九转" not in f                  # n=0 的信号不喂入，省 token


def test_generate_scout_note_and_cache() -> None:
    tmp = Path(tempfile.mkdtemp())
    S._note_cache_dir = lambda: tmp           # type: ignore[assignment]
    fake = _FakeLLM("这只票趋势性强，历史上均线多头排列(n=18)期望最高，更吃趋势跟随；样本充足但历史≠未来。")
    out = S.generate_scout_note(_scout_result(), client=fake)
    assert out["ok"] and "均线多头" in out["note"] and out["model"] and out["disclaimer"]
    assert fake.calls == 1
    # 相同 facts → 命中缓存，不再调用模型
    out2 = S.generate_scout_note(_scout_result(), client=fake)
    assert out2["note"] == out["note"] and fake.calls == 1


def test_generate_note_requires_ok() -> None:
    out = S.generate_scout_note({"ok": False}, client=_FakeLLM("x"))
    assert out["ok"] is False


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
