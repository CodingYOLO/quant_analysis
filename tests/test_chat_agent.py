"""AI投研 chat agent 工具的「数据可溯源」回归测试（纯逻辑·零网络·假 provider）。

锁死曾反复出错的根因：工具把现价/日期/来源**剥掉**喂给 LLM，导致模型用记忆里的
旧价(把 74 元说成 28 元)还谎称"实时"。这些字段一旦被改没，本测试立即失败。
"""

from __future__ import annotations

import pandas as pd

import app.strategy.chat_agent as CA


class _FakeProvider:
    """只实现 chat 工具用到的少数方法。"""

    def get_stock_basic(self) -> pd.DataFrame:
        return pd.DataFrame([{"ts_code": "002156.SZ", "name": "通富微电", "industry": "半导体"}])

    def get_realtime_quote(self, codes) -> pd.DataFrame:
        return pd.DataFrame([{"ts_code": "002156.SZ", "name": "通富微电",
                              "price": 74.64, "pct_chg": 9.09, "prev_close": 68.42}])


def test_quote_tool_carries_realtime_provenance() -> None:
    """stock_quote 必须带：实时现价 + 数据时间 + 新浪来源标注（防止 LLM 用记忆旧价）。"""
    out = CA._t_quote({"stock": "通富微电"}, _FakeProvider())
    assert out["现价"] == 74.64 and out["涨跌幅%"] == 9.09 and out["昨收"] == 68.42
    assert "数据时间" in out and out["数据时间"]
    assert "新浪实时" in out["来源"] and "现价" in out["来源"]


def test_system_prompt_enforces_freshness_rules() -> None:
    """system prompt 必须保留：现价只用 stock_quote、禁记忆旧价、禁编目标价、禁脑补派生指标。"""
    s = CA._SYSTEM
    assert "stock_quote" in s
    assert "训练记忆里的股价" in s            # 禁用记忆里的旧价
    assert "派生指标" in s                    # 禁脑补"近20日涨幅"等
    assert "绝不编造一个" in s                # 工具没返回的数字不许编


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_chat_agent 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
