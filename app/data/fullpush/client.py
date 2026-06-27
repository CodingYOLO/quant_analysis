"""幕数据沪深全推 TCP 客户端：长连接收流 → 解析 → 写入 MarketSnapshot。

协议：连接后立即发送 token(UTF-8)；之后服务端持续下推，每条报文为
4字节小端无符号长度前缀 + UTF-8 正文。断线指数退避重连。后台线程运行，
失败不影响主流程（分析层用 snapshot.is_stale 触发新浪兜底）。
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
from collections.abc import Callable

from app.data.fullpush.parser import parse_message
from app.data.fullpush.snapshot import MarketSnapshot

logger = logging.getLogger(__name__)

_LEN_PREFIX = 4
_MAX_MSG = 1 << 20          # 单报文上限 1MB（防异常长度撑爆内存）
_BACKOFF_MAX = 30.0        # 重连退避上限（秒）
_RECV_TIMEOUT = 30.0       # 收流静默超时（秒）→ 视为断线重连


class FullPushClient:
    """全推长连接客户端（依赖注入 host/port/token + snapshot）。"""

    def __init__(self, host: str, port: int, token: str, snapshot: MarketSnapshot,
                 *, on_status: Callable[[str], None] | None = None) -> None:
        self._host, self._port, self._token = host, port, token
        self._snap = snapshot
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_settings(cls, snapshot: MarketSnapshot, **kw) -> "FullPushClient":
        """从配置(.env)构造：fullpush_host/port/token。"""
        from app.config import get_settings
        s = get_settings()
        return cls(s.fullpush_host, s.fullpush_port, s.fullpush_token, snapshot, **kw)

    # ---- 生命周期 ----
    def start(self) -> None:
        """启动后台收流线程（幂等）。"""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fullpush", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 内部 ----
    def _status(self, msg: str) -> None:
        logger.info("[全推] %s", msg)
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def _run(self) -> None:
        """重连主循环：断线指数退避，stop 时退出。"""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect_and_consume()
                backoff = 1.0
            except Exception as e:
                self._status(f"连接中断，{backoff:.0f}s 后重连：{e}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    def _connect_and_consume(self) -> None:
        with socket.create_connection((self._host, self._port), timeout=10) as sock:
            sock.sendall(self._token.encode("utf-8"))
            self._status(f"已连接 {self._host}:{self._port}")
            sock.settimeout(_RECV_TIMEOUT)
            while not self._stop.is_set():
                payload = self._recv_message(sock)
                if payload is None:
                    raise ConnectionError("服务端关闭连接")
                self._snap.update_many(parse_message(payload))

    def _recv_message(self, sock: socket.socket) -> str | None:
        """读一条【长度前缀 + 正文】报文；连接关闭返回 None。"""
        head = self._recvall(sock, _LEN_PREFIX)
        if head is None:
            return None
        length = struct.unpack("<I", head)[0]
        if not 0 < length <= _MAX_MSG:
            raise ValueError(f"异常报文长度 {length}")
        body = self._recvall(sock, length)
        return body.decode("utf-8", "replace") if body is not None else None

    @staticmethod
    def _recvall(sock: socket.socket, n: int) -> bytes | None:
        """精确读取 n 字节；对端关闭返回 None。"""
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)
