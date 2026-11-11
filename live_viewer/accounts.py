"""Login verification and ordered credential writes, independent of any GUI."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import threading

from .core import PlatformClient, ViewerError


def verify_login(platform, jar, *, network_mode="direct"):
    client = PlatformClient(platform, jar, network_mode=network_mode)
    try:
        status = client.login_status()
        if status["verified"] is False:
            raise ViewerError(status["message"], code="account.expired", level="WARN")
        return status
    finally:
        client.close()


@dataclass
class CredentialRequest:
    platform: str
    generation: int
    jar: object = field(repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)


class CredentialWriter:
    """Serialize disk changes; newer clears always follow any in-flight save.

    Request creation never waits on disk. A newer request makes not-yet-started
    older writes obsolete. Each disk lock belongs to one platform only.
    """
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.io_locks = {}
        self.generations = {}
        self.pending = []

    def request(self, platform, jar=None):
        with self.lock:
            version = self.generations.get(platform, 0) + 1
            self.generations[platform] = version
            self.io_locks.setdefault(platform, threading.Lock())
            request = CredentialRequest(platform, version, copy.deepcopy(jar))
            self.pending = [r for r in self.pending if not r.done.is_set()]
            self.pending.append(request)
            return request

    def apply(self, request):
        try:
            with self.io_locks[request.platform]:
                with self.lock:
                    if self.generations[request.platform] != request.generation:
                        return False
                if request.jar is None:
                    self.store.delete(request.platform)
                else:
                    self.store.save(request.platform, request.jar)
                return True
        finally:
            request.jar = None
            request.done.set()

    def close(self):
        with self.lock:
            pending = tuple(self.pending)
        for request in pending:
            request.done.wait(15)
