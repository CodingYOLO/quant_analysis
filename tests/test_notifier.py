"""Bark 推送——多设备 key 解析单测（纯函数·不连网）。"""

from __future__ import annotations

from app.notify.notifier import _bark_keys


def test_single_key_backward_compatible() -> None:
    assert _bark_keys("myKey123") == ["myKey123"]


def test_multi_device_split() -> None:
    assert _bark_keys("myKey,dadKey") == ["myKey", "dadKey"]


def test_strip_and_dedup_and_order() -> None:
    # 去空格 + 去重 + 保序（先来先留）
    assert _bark_keys(" myKey , dadKey , myKey ") == ["myKey", "dadKey"]


def test_empty_and_blank_yield_nothing() -> None:
    assert _bark_keys("") == []
    assert _bark_keys(None) == []          # type: ignore[arg-type]
    assert _bark_keys(" , , ") == []       # 全空段过滤掉


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_notifier 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
