from __future__ import annotations

import time


class FlvClock:
    """Observe complete audio/video tag DTS; never alter or decode media bytes."""

    def __init__(self):
        self.pending = bytearray()
        self.header = False
        self.valid = True
        self.first = self.latest = self.started = None
        self.raw_timestamp = None
        self.discontinuities = 0

    def feed(self, chunk: bytes, now: float | None = None):
        if not self.valid:
            return
        now = time.monotonic() if now is None else now
        self.pending.extend(chunk)
        cursor = 0
        if not self.header:
            if len(self.pending) < 9:
                return
            offset = int.from_bytes(self.pending[5:9], "big")
            if self.pending[:3] != b"FLV" or not 9 <= offset <= 65536:
                self.valid = False
                self.pending.clear()
                return
            if len(self.pending) < offset + 4:
                return
            cursor = offset + 4
            self.header = True
        while cursor + 11 <= len(self.pending):
            kind = self.pending[cursor] & 31
            size = int.from_bytes(self.pending[cursor + 1:cursor + 4], "big")
            end = cursor + 11 + size
            if end + 4 > len(self.pending):
                break
            payload = self.pending[cursor + 11:end]
            # Ignore AVC/AAC configuration tags, commonly stamped zero.
            is_media = (kind == 9 and len(payload) >= 2 and payload[0] & 15 == 7 and payload[1] == 1)
            is_media = is_media or (kind == 8 and payload and (payload[0] >> 4 != 10 or len(payload) >= 2 and payload[1] == 1))
            if is_media:
                stamp = int.from_bytes(self.pending[cursor + 4:cursor + 7], "big") | self.pending[cursor + 7] << 24
                if self.raw_timestamp is None:
                    self.raw_timestamp = stamp
                    self.first = self.latest = stamp
                    self.started = now
                else:
                    delta = ((stamp - self.raw_timestamp + 2**31) % 2**32) - 2**31
                    if delta < -2000:
                        self.discontinuities += 1
                        self.first = self.latest = stamp
                        self.started = now
                        self.raw_timestamp = stamp
                    elif delta >= 0:
                        self.latest += delta
                        self.raw_timestamp = stamp
            cursor = end + 4
        del self.pending[:cursor]
        if len(self.pending) > 16 * 1024 * 1024 + 15:
            self.valid = False
            self.pending.clear()

    def snapshot(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        span = (self.latest - self.first) / 1000 if self.first is not None else None
        elapsed = now - self.started if self.started is not None else None
        return {"media_seconds": round(span, 3) if span is not None else None,
                "clock_drift_seconds": round(elapsed - span, 3) if span is not None else None,
                "timestamp_discontinuities": self.discontinuities}
