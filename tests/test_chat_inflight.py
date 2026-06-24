"""AI 投研「后台不中断」生成的单测（纯逻辑·零网络·假 runner/saver）。

核心要验证：即使没有任何客户端消费事件（模拟用户切走、连接断开），
后台生成也能跑完并把答案存库——这正是「切走再回来能看到回答」的根基。
"""

from __future__ import annotations

import time

from app.strategy.chat_inflight import InflightRegistry


def _fake_runner(deltas):
    """构造一个产出给定 delta 文本（夹带 status）的假生成器。"""
    def run(_hist):
        yield {"type": "status", "text": "查数据中…"}
        for d in deltas:
            yield {"type": "delta", "text": d}
    return run


def _wait_done(job, timeout=3.0):
    t0 = time.time()
    while not job.done and time.time() - t0 < timeout:
        time.sleep(0.01)
    return job.done


def test_completes_and_saves_without_consumer() -> None:
    """无人消费事件流，后台仍跑完并落库（模拟客户端断开）。"""
    saved: list[tuple] = []
    reg = InflightRegistry(_fake_runner(["你", "好", "世界"]),
                           lambda sid, role, content: saved.append((sid, role, content)))
    job = reg.start(7, [{"role": "user", "content": "hi"}])
    assert _wait_done(job), "后台生成应在超时内完成"
    assert job.answer == "你好世界"
    assert saved == [(7, "assistant", "你好世界")]
    assert job.events[-1] == {"type": "end"}


def test_is_active_transitions() -> None:
    """生成期间 is_active=True，完成后 False。"""
    def slow_runner(_hist):
        yield {"type": "delta", "text": "a"}
        time.sleep(0.15)
        yield {"type": "delta", "text": "b"}
    reg = InflightRegistry(slow_runner, lambda *a: None)
    job = reg.start(1, [])
    assert reg.is_active(1) is True
    assert _wait_done(job)
    assert reg.is_active(1) is False


def test_reuses_active_job() -> None:
    """同一会话在途时再次 start 复用同一任务，不重复生成。"""
    calls = {"n": 0}

    def counting_runner(_hist):
        calls["n"] += 1
        time.sleep(0.1)
        yield {"type": "delta", "text": "x"}

    reg = InflightRegistry(counting_runner, lambda *a: None)
    j1 = reg.start(3, [])
    j2 = reg.start(3, [])                 # 在途 → 应复用
    assert j1 is j2
    assert _wait_done(j1)
    assert calls["n"] == 1


def test_runner_exception_is_captured() -> None:
    """生成抛异常也要收尾：done=True、有 error 事件、不悬挂。"""
    def boom(_hist):
        yield {"type": "delta", "text": "半句"}
        raise RuntimeError("模型超时")
    saved: list[tuple] = []
    reg = InflightRegistry(boom, lambda sid, role, content: saved.append((sid, role, content)))
    job = reg.start(9, [])
    assert _wait_done(job)
    assert job.error and "模型超时" in job.error
    assert any(e["type"] == "error" for e in job.events)
    assert job.events[-1] == {"type": "end"}
    assert saved == [(9, "assistant", "半句")]      # 已生成的半句仍落库，不丢


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_chat_inflight 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
