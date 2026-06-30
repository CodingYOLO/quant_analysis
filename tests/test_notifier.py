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


def _patch_keys(bark_key: str, user2: str) -> None:
    """临时把 settings 的两人 key 打桩（测试路由用·进程隔离）。"""
    import app.config as cfg

    class _S:
        pass
    s = _S()
    s.bark_key, s.bark_key_user2 = bark_key, user2
    cfg.get_settings = lambda: s            # type: ignore[assignment]


def test_all_device_keys_union() -> None:
    from app.notify.notifier import all_device_keys
    _patch_keys("A", "B")
    assert all_device_keys() == "A,B"        # 全市场→两台
    _patch_keys("A", "")
    assert all_device_keys() == "A"          # 用户2未接入→只我
    _patch_keys("", "")
    assert all_device_keys() == ""


def test_owner_device_keys_routing() -> None:
    from app.notify.notifier import owner_device_keys
    _patch_keys("A", "B")
    assert owner_device_keys(["me"]) == "A"          # 我的自选→只我
    assert owner_device_keys(["dad"]) == "B"         # 爸爸的→只爸爸
    assert owner_device_keys(["me", "dad"]) == "A,B"  # 两人都关注→都推
    _patch_keys("A", "")
    assert owner_device_keys(["dad"]) == ""          # 爸爸没配设备→空(调用方跳过·不回落给我)


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_notifier 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
