"""公司画像：硬事实抽取 + LLM 归纳的单测（纯逻辑·零网络·假 provider/client）。"""

from __future__ import annotations

import uuid

import pandas as pd

import app.strategy.company_profile as CP


class _FakeProvider:
    def get_stock_company(self, ts: str) -> pd.DataFrame:
        return pd.DataFrame([{"main_business": "集成电路封装测试",
                              "introduction": "通富微电专业从事集成电路封装测试。",
                              "employees": 25446}])

    def get_main_business(self, ts: str) -> pd.DataFrame:
        return pd.DataFrame([
            {"end_date": "20251231", "bz_item": "集成电路封装测试", "bz_sales": 27247567100.0},
            {"end_date": "20251231", "bz_item": "模具及材料", "bz_sales": 673857555.0},
            {"end_date": "20240630", "bz_item": "旧期数据应被过滤", "bz_sales": 1.0},
        ])

    def get_stock_basic(self) -> pd.DataFrame:
        return pd.DataFrame([{"ts_code": "002156.SZ", "industry": "半导体"}])


class _FakeClient:
    def __init__(self, payload: str) -> None:
        self._p = payload

    def chat(self, messages, **kw) -> str:
        return self._p


def test_parse_obj() -> None:
    assert CP._parse_obj('{"a":1}') == {"a": 1}
    assert CP._parse_obj('```json\n{"a":2}\n```') == {"a": 2}
    assert CP._parse_obj("没有json") is None
    assert CP._parse_obj("") is None


def test_fmt_yi() -> None:
    assert CP._fmt_yi(1e8) == 1.0
    assert CP._fmt_yi(2.5e9) == 25.0
    assert CP._fmt_yi(None) is None


def test_gather_facts_computes_pct_and_filters_old() -> None:
    """主营构成取最新报告期、占比合计≈100、旧期被过滤。"""
    f = CP._gather_facts("002156.SZ", "通富微电", _FakeProvider())
    assert f["主营业务"] == "集成电路封装测试" and f["行业"] == "半导体"
    comp = f["主营构成"]
    assert len(comp) == 2 and comp[0]["产品"] == "集成电路封装测试"   # 旧期(20240630)被过滤
    assert abs((comp[0]["占比"] or 0) + (comp[1]["占比"] or 0) - 100) < 0.5
    assert comp[0]["营收亿"] == 272.48 and f["构成报告期"] == "2025年报"


def test_build_company_profile_injected() -> None:
    """端到端(假 client·禁联网)：硬事实 + LLM 归纳字段齐全。"""
    orig = CP._web_research
    CP._web_research = lambda name, max_items=9: []      # 禁联网
    try:
        ts = uuid.uuid4().hex[:6] + ".SZ"                # 唯一 → 避开按月磁盘缓存
        fake = _FakeClient('{"定位":"封测龙头","行业地位":"国内前三[1]",'
                           '"全球排名":"公开资料未见明确全球排名",'
                           '"核心竞争力":["AMD核心封测伙伴"],"局限与风险":["客户集中"]}')
        out = CP.build_company_profile(ts, "通富微电", provider=_FakeProvider(), client=fake)
        assert out["ok"] and out["定位"] == "封测龙头"
        assert out["核心竞争力"] == ["AMD核心封测伙伴"] and out["局限与风险"] == ["客户集中"]
        assert out["主营业务"] == "集成电路封装测试" and len(out["主营构成"]) == 2
        assert out["sources"] == [] and out["disclaimer"]
    finally:
        CP._web_research = orig


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_company_profile 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
