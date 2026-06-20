"""
回测·AI 综合研判 llm_brief 单测（纯函数 + 注入 fake client，零网络）。

可直接运行：python -m tests.test_llm_brief
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import app.backtest.llm_brief as B


def _payload() -> dict:
    """模拟前端 POST 的已算好结构化结果（horizons 键为 str，仿 json 往返）。"""
    return {
        "name": "贵州茅台", "signal_label": "MACD金叉",
        "result": {
            "ts_code": "600519.SH", "signal_label": "MACD金叉", "start": "20230101", "end": "20250101",
            "n_signals": 19,
            "horizons": {"1": {"n": 19, "win_rate": 0.52, "avg_return": 0.3, "profit_factor": 1.1},
                         "5": {"n": 19, "win_rate": 0.55, "avg_return": 0.5, "profit_factor": 1.2}},
            "index_label": "沪深300", "current_regime": "弱势",
            "regime_window": {"强势": 29.8, "震荡": 22.9, "弱势": 47.3},
            "by_regime": {"强势": {"n": 5, "horizons": {"5": {"n": 5, "win_rate": 0.6, "avg_return": 2.17, "profit_factor": 1.5}}}},
        },
        "sector": {
            "industry": "白酒", "n_peers": 18, "n_occ": 314,
            "pooled": {"5": {"n": 314, "win_rate": 0.449, "avg_return": 0.48, "profit_factor": 1.23}},
            "current_breadth": {"pct_ma20": 11.1, "pct_ma5": 44.4},
            "by_breadth": {"板块强(≥60%)": {"n": 141, "horizons": {"5": {"n": 141, "win_rate": 0.362, "avg_return": -0.1, "profit_factor": 0.9}}}},
        },
        "profile": {"name": "贵州茅台", "tags": [{"text": "低波动"}, {"text": "趋势性强"}],
                    "chip": {"weight_avg": 1500, "ref_close": 1450, "premium": -3.3, "winner_rate": 20}},
        "fundamentals": {"summary": "ROE高、负债低",
                         "forecast": {"type": "预增", "net_change": "+10%", "period": "2024年报"}},
        "news": {"summary": "近一月无重大解禁；机构调研活跃；行业价格指数走稳。",
                 "sources": [{"site": "财联社", "date": "2026-06-10"}]},
    }


class _FakeLLM:
    """计数 fake client：返回固定回复，便于验证缓存命中后不再调用。"""
    def __init__(self, reply: str):
        self.reply, self.calls = reply, 0

    def chat(self, messages, task_type="flash", **kw):
        self.calls += 1
        return self.reply


_GOOD_JSON = ('{"stance":"中性偏谨慎","summary":"回测胜率尚可但盈亏比偏低，叠加板块深度走弱，'
              '当下宜观望。","supports":["同类T+5胜率44.9%(n=314)偏低"],'
              '"risks":["板块广度仅11%，样本薄需谨慎"],"todos":["确认有无业绩雷"]}')


def _use_temp_cache():
    tmp = Path(tempfile.mkdtemp())
    B._cache_dir = lambda: tmp  # type: ignore[assignment]


def test_build_facts_covers_all_blocks() -> None:
    f = B.build_facts(_payload())
    assert "MACD金叉" in f and "T+5" in f
    assert "大盘状态" in f and "弱势" in f
    assert "白酒" in f and "站上MA20 11.1%" in f
    assert "板块强(≥60%)" in f
    assert "低波动" in f and "筹码" in f
    assert "业绩预告" in f and "预增" in f
    assert "近期新闻" in f and "解禁" in f       # 新闻已喂入供研判核查消息面


def test_parse_brief_clean_and_messy() -> None:
    d = B.parse_brief(_GOOD_JSON)
    assert d["stance"] == "中性偏谨慎" and "观望" in d["summary"]
    assert len(d["supports"]) == 1 and len(d["risks"]) == 1
    # 带代码块/前后缀也能提取
    d2 = B.parse_brief("```json\n" + _GOOD_JSON + "\n```  以上。")
    assert d2["stance"] == "中性偏谨慎"
    # 非 JSON → 兜底把原文塞进 supports，不抛错
    d3 = B.parse_brief("模型乱答一通没有大括号")
    assert d3["stance"] == "" and d3["supports"] and "模型乱答" in d3["supports"][0]


def test_generate_brief_and_cache() -> None:
    _use_temp_cache()
    fake = _FakeLLM(_GOOD_JSON)
    out = B.generate_brief(_payload(), client=fake)
    assert out["ok"] and out["stance"] == "中性偏谨慎" and "观望" in out["summary"]
    assert out["risks"] and out["model"] and out["disclaimer"]
    assert fake.calls == 1
    # 相同 facts → 命中缓存，不再调用模型
    out2 = B.generate_brief(_payload(), client=fake)
    assert out2["stance"] == "中性偏谨慎" and fake.calls == 1


def test_generate_brief_requires_signals() -> None:
    _use_temp_cache()
    bad = {"result": {"ts_code": "X", "n_signals": 0}}
    out = B.generate_brief(bad, client=_FakeLLM(_GOOD_JSON))
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
