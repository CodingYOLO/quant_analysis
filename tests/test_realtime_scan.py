"""盯盘推送去重：冷却 + 升级逻辑单测（零网络）。"""

from __future__ import annotations

from app.strategy import realtime_scan as rs


def test_cooldown_sec_by_prefix() -> None:
    assert rs._cooldown_sec("crash_600519.SH") == 600           # 风险·复发快报
    assert rs._cooldown_sec("secout_工业金属_15") == 1500       # 板块·档位后缀不影响前缀
    assert rs._cooldown_sec("surge_300308.SZ") == 1200          # 个股机会
    assert rs._cooldown_sec("whatever_x") == rs._COOLDOWN_DEFAULT


def test_should_push_cooldown() -> None:
    """冷却内不重复；过冷却=再提醒一次。"""
    rs._pushed.clear()
    now = 1_000_000.0
    assert rs._should_push("crash_A", now) is True              # 没推过 → 推
    rs._pushed["crash_A"] = now
    assert rs._should_push("crash_A", now + 300) is False       # 300s < 600 冷却内
    assert rs._should_push("crash_A", now + 600) is True        # 到冷却 → 再提醒
    rs._pushed.clear()


def test_mag_tier_escalation() -> None:
    """量级跨档 → key 变化 → 绕过冷却立即再推。"""
    assert rs._mag_tier(-3.2) == 3
    assert rs._mag_tier(-16) == 15
    assert rs._mag_tier(-50) == 40
    assert f"secout_X_{rs._mag_tier(-3)}" != f"secout_X_{rs._mag_tier(-16)}"   # 升级=新key


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_realtime_scan 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
