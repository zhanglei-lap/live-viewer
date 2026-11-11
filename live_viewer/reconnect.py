from __future__ import annotations

import time


class ReconnectPolicy:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.attempts = self.total_attempts = 0
        self.due = None
        self.active = self.paused = False

    def start(self):
        self.active, self.paused = True, False
        self.attempts = 0
        self.due = None

    def failure(self):
        if not self.active or self.paused or self.due is not None:
            return
        delay = (1, 2, 5, 10, 20, 30)[min(self.attempts, 5)]
        self.attempts += 1
        self.due = self.clock() + delay

    def ready(self):
        self.attempts = 0
        self.due = None

    def consume(self):
        if self.active and not self.paused and self.due is not None and self.clock() >= self.due:
            self.due = None
            self.total_attempts += 1
            return True
        return False

    def stop(self):
        self.active = False
        self.due = None
