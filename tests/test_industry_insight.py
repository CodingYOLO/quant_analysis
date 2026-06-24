"""产业认知：LLM 输出 JSON 数组的鲁棒解析单测(纯函数·零网络)。"""

from __future__ import annotations

import app.strategy.industry_insight as II


def test_parse_clean_array() -> None:
    qs = II._parse_json_array('[{"q":"题1","point":"考点1"},{"q":"题2","point":"考点2"}]')
    assert qs and len(qs) == 2 and qs[0]["q"] == "题1"


def test_parse_with_code_fence() -> None:
    raw = '```json\n[{"q":"a","point":"b"}]\n```'
    assert II._parse_json_array(raw) == [{"q": "a", "point": "b"}]


def test_parse_with_leading_prose() -> None:
    raw = '好的，以下是题目：\n[{"q":"x","point":"y"}]\n希望有帮助'
    assert II._parse_json_array(raw) == [{"q": "x", "point": "y"}]


def test_parse_bad_returns_none() -> None:
    assert II._parse_json_array("没有数组") is None
    assert II._parse_json_array("") is None
    assert II._parse_json_array("[坏json,]") is None


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_industry_insight 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
