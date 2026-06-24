"""AI 投研问答的「后台不中断」生成器。

问题：原来生成直接挂在 HTTP 流式响应里，用户切到别的页面 → 浏览器断开连接
→ 生成器被关闭、答案没存库就丢了。

方案：把生成放进**后台线程**跑到完成并落库，HTTP 的 SSE 只是「观察者」——
断开/重连都不影响后台生成。用户切走再回来，要么看到已存好的答案，要么重新
接上仍在进行的流。

设计要点：
- 依赖注入 `runner`(=run_chat) 与 `saver`(=db.add_chat_message)，便于单测、不耦合具体实现。
- 按 session_id 维护在途任务，线程安全；同一会话已有在途则复用，避免重复生成。
- 完成后保留一段宽限期，供「晚到的重连客户端」取用最终结果。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

# 事件迭代器：run_chat(hist) 产出形如 {"type": "delta"|"status"|"thinking"|"error", "text": str}
RunnerFn = Callable[[list[dict]], Iterator[dict]]
# 落库：db.add_chat_message(session_id, role, content)
SaverFn = Callable[[int, str, str], None]


class InflightJob:
    """一次在途生成的共享状态。后台线程写、SSE 观察者读（只追加不删除，GIL 下安全）。"""

    __slots__ = ("events", "answer", "done", "error", "started")

    def __init__(self) -> None:
        self.events: list[dict] = []      # 完整事件序列（含 status/delta/error/end），供重连回放
        self.answer: str = ""             # 累计正文
        self.done: bool = False           # 生成是否结束（成功或失败）
        self.error: str | None = None
        self.started: float = time.time()


class InflightRegistry:
    """按 session_id 管理「后台不中断」的问答生成。线程安全。"""

    def __init__(self, runner: RunnerFn, saver: SaverFn, grace_seconds: int = 180) -> None:
        self._runner = runner
        self._saver = saver
        self._grace = max(1, grace_seconds)       # 完成后保留时长（秒），供晚到重连
        self._lock = threading.Lock()
        self._jobs: dict[int, InflightJob] = {}

    # ── 查询 ────────────────────────────────────────────────────────────
    def get(self, sid: int) -> InflightJob | None:
        with self._lock:
            return self._jobs.get(sid)

    def is_active(self, sid: int) -> bool:
        """该会话是否正在后台生成（尚未完成）。"""
        job = self.get(sid)
        return bool(job and not job.done)

    # ── 启动 ────────────────────────────────────────────────────────────
    def start(self, sid: int, hist: list[dict]) -> InflightJob:
        """启动后台生成；若该会话已有在途任务则直接复用，避免重复跑、重复扣费。"""
        with self._lock:
            existing = self._jobs.get(sid)
            if existing is not None and not existing.done:
                return existing
            job = InflightJob()
            self._jobs[sid] = job
        threading.Thread(target=self._work, args=(sid, hist, job),
                         name=f"chat-gen-{sid}", daemon=True).start()
        return job

    # ── 后台线程主体 ────────────────────────────────────────────────────
    def _work(self, sid: int, hist: list[dict], job: InflightJob) -> None:
        """跑完整轮生成：累计正文 → 落库 → 标记完成。任何异常都要收尾，绝不悬挂。"""
        parts: list[str] = []
        try:
            for ev in self._runner(hist):
                if ev.get("type") == "delta":
                    parts.append(ev.get("text", ""))
                job.events.append(ev)
        except Exception as e:                              # 生成中途失败也要落地
            job.error = str(e)[:160]
            job.events.append({"type": "error", "text": job.error})
            logger.warning("[chat-inflight] 会话 %s 生成异常: %s", sid, e)
        job.answer = "".join(parts)
        if job.answer:
            try:
                self._saver(sid, "assistant", job.answer)
            except Exception as e:
                logger.error("[chat-inflight] 会话 %s 存库失败: %s", sid, e)
        job.events.append({"type": "end"})
        job.done = True
        self._schedule_cleanup(sid)

    def _schedule_cleanup(self, sid: int) -> None:
        """完成后过宽限期再清理，避免晚到的重连客户端取不到最终结果。"""
        def _cleanup() -> None:
            time.sleep(self._grace)
            with self._lock:
                job = self._jobs.get(sid)
                if job is not None and job.done:
                    self._jobs.pop(sid, None)

        threading.Thread(target=_cleanup, name=f"chat-gc-{sid}", daemon=True).start()
