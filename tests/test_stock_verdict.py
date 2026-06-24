"""个股360 综合判断 stock_verdict 单测：事实拼装 + JSON解析 + 评分裁剪 + 注入式生成。

零网络（注入 fake client）。直接运行：python -m tests.test_stock_verdict
"""

from __future__ import annotations

import app.strategy.stock_verdict as V


# ---- 1. 事实拼装 build_facts ----

def test_build_facts_order_and_skip_empty() -> None:
    s = {"资金": "主力+5亿", "行情": "现价100 +2%", "板块": "", "自定义区": "x"}
    txt = V.build_facts(s)
    assert txt.index("【行情】") < txt.index("【资金】")   # 按预设顺序
    assert "【板块】" not in txt                          # 空区跳过
    assert "【自定义区】" in txt                          # 额外区保留


# ---- 2. 评分裁剪 ----

def test_clamp_score() -> None:
    assert V._clamp_score(72) == 72
    assert V._clamp_score("85") == 85
    assert V._clamp_score(120) == 100 and V._clamp_score(-5) == 0
    assert V._clamp_score(None) is None and V._clamp_score("x") is None


# ---- 3. JSON 解析 ----

def test_parse_verdict_ok() -> None:
    raw = '前言{"stance":"值得关注","score":78,"summary":"强","bulls":["净利+224%"],"risks":["乖离16%"],"plan":["回踩MA20买"]}尾'
    d = V.parse_verdict(raw)
    assert d["stance"] == "值得关注" and d["score"] == 78
    assert d["bulls"] == ["净利+224%"] and d["risks"] == ["乖离16%"] and d["plan"] == ["回踩MA20买"]


def test_parse_verdict_fallback() -> None:
    d = V.parse_verdict("模型没按格式输出")
    assert d["stance"] == "" and d["score"] is None and d["summary"] == "模型没按格式输出"


# ---- 4. 端到端 build_verdict（注入 fake client·零网络） ----

class _FakeClient:
    def __init__(self, payload): self._p = payload
    def chat(self, messages, **kw):
        assert "data" not in kw  # 仅确认被调用
        return self._p


def test_build_verdict_injected(tmpok=True) -> None:
    fake = _FakeClient('{"stance":"观望","score":55,"summary":"板块退潮","bulls":["ROE 18%"],"risks":["机构净卖2亿"],"plan":["等回踩"]}')
    out = V.build_verdict("兆易创新", "603986.SH", {"资金": "主力+5亿 但龙虎榜机构净卖2亿"}, client=fake)
    assert out["ok"] and out["stance"] == "观望" and out["score"] == 55
    assert "机构净卖2亿" in out["risks"][0]
    assert out["disclaimer"]


def test_build_verdict_empty_facts() -> None:
    out = V.build_verdict("x", "000001.SZ", {}, client=_FakeClient("{}"))
    assert not out["ok"]


class _FlakyClient:
    """首调返回不可解析(模拟 API 抖动/截断)，次调返回正常 JSON。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, **kw) -> str:
        self.calls += 1
        return ("服务繁忙，请稍后" if self.calls == 1
                else '{"stance":"值得关注","score":70,"summary":"ok","bulls":[],"risks":[],"plan":[]}')


def test_build_verdict_retries_on_unparseable() -> None:
    """首次解析不出 stance → 自动重试一次后成功（用唯一 facts 避开缓存）。"""
    import uuid
    fake = _FlakyClient()
    out = V.build_verdict("通富微电", "002156.SZ", {"资金": "唯一" + uuid.uuid4().hex}, client=fake)
    assert fake.calls == 2 and out["ok"] and out["stance"] == "值得关注"


def test_build_verdict_both_fail_empty_stance() -> None:
    """两次都解析失败 → ok=True 但 stance 为空（前端据此显示"生成失败"+重试按钮）。"""
    import uuid
    out = V.build_verdict("x", "002156.SZ", {"资金": "唯一" + uuid.uuid4().hex},
                          client=_FakeClient("乱七八糟没有JSON"))
    assert out["ok"] and out["stance"] == ""


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_stock_verdict 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
