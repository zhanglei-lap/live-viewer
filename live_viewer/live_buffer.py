from __future__ import annotations

import threading
import time
from collections import deque

from .core import ViewerError


class RelayBacklog(ViewerError):
    def __init__(self):
        super().__init__("播放器读取落后，已停止旧直播流；点击“回到直播”重新连接。")


class LiveBuffer:
    """A bounded FIFO; abort a stale connection without dropping/splicing bytes."""

    def __init__(self, *, max_bytes=8 * 1024 * 1024, max_age=3.0, clock=time.monotonic):
        self.max_bytes, self.max_age, self.clock = max_bytes, max_age, clock
        self.condition = threading.Condition()
        self.pending = deque()
        self.size = self.peak_size = 0
        self.peak_age = 0.0
        self.finished = self.cancelled = False
        self.error = None

    def _check_age(self):
        age = max(0.0, self.clock() - self.pending[0][0]) if self.pending else 0.0
        self.peak_age = max(self.peak_age, age)
        if age > self.max_age:
            self.error = RelayBacklog()
            self.finished = True
        return age

    def put(self, chunk):
        if not chunk:
            return
        with self.condition:
            if self.cancelled or self.finished:
                return
            self._check_age()
            if self.size + len(chunk) > self.max_bytes:
                self.error = RelayBacklog()
                self.finished = True
            if not self.finished:
                self.pending.append((self.clock(), chunk))
                self.size += len(chunk)
                self.peak_size = max(self.peak_size, self.size)
            self.condition.notify_all()
            if self.error:
                raise self.error

    def get(self):
        with self.condition:
            while True:
                if self.cancelled:
                    return None
                self._check_age()
                if self.error:
                    raise self.error
                if self.pending:
                    _, chunk = self.pending.popleft()
                    self.size -= len(chunk)
                    return chunk
                if self.finished:
                    return None
                self.condition.wait(0.1)

    def finish(self, error=None):
        with self.condition:
            self.finished = True
            if error is not None and self.error is None:
                self.error = error
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.cancelled = True
            self.pending.clear()
            self.size = 0
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            age = max(0.0, self.clock() - self.pending[0][0]) if self.pending else 0.0
            return {"pending_bytes": self.size, "pending_seconds": round(age, 3),
                    "peak_pending_bytes": self.peak_size,
                    "peak_pending_seconds": round(max(self.peak_age, age), 3)}
