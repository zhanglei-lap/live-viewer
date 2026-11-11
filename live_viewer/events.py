"""Framework independent, privacy filtered events, durable storage and notices."""
from __future__ import annotations

import json
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
IMPACTS = ("none", "degraded", "start_blocked", "interrupted", "app_blocked")
PRESENTATIONS = ("log", "toast", "banner", "strong")
DETAIL_KEYS = {"host", "stage", "reason", "attempt", "network_mode", "event_count"}
_URL = re.compile(r"""(?i)\b(?:https?|rtmps?|wss?)://[^\s<>"'，；）]+""")
_SECRET = re.compile(
    r"(?i)\b(?:cookie|set-cookie|authorization|sessdata|bili_jct|acf_[\w]+|"
    r"qrcode_key|qr_key|auth|enc_data|token|access_token|refresh_token|signature|sign)"
    r"\s*[:=]\s*[^\r\n]+")


def safe_text(value, limit=500):
    """Defence in depth for user labels; raw exceptions/objects are never logged."""
    if not isinstance(value, str):
        return "[非文本内容已隐藏]"
    value = _SECRET.sub("[凭据已隐藏]", value)
    value = _URL.sub("[请求地址已隐藏]", value)
    value = "".join(c for c in value if c >= " " or c == "\n")
    return value[:limit] or "—"


def safe_details(details):
    clean = {}
    if details is not None and not isinstance(details, (dict, MappingProxyType)):
        raise ValueError("Event details must be a mapping")
    for key, value in (details or {}).items():
        if key not in DETAIL_KEYS:
            continue  # Never serialize jars, headers, URLs or arbitrary nested data.
        if key in ("attempt", "event_count"):
            if type(value) is int and value >= 0:
                clean[key] = value
        elif key == "network_mode":
            if value in ("direct", "system"):
                clean[key] = value
        elif key == "host":
            if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,252}", value):
                clean[key] = value.lower()
        elif isinstance(value, str):
            clean[key] = safe_text(value, 80 if key == "stage" else 100)
    return MappingProxyType(clean)


@dataclass(frozen=True)
class Event:
    code: str
    message: str
    source: str = "app"
    level: str = "INFO"
    impact: str = "none"
    presentation: str = "log"
    target: str | None = None
    session_id: str | None = None
    dedupe_key: str | None = None
    details: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="milliseconds"))
    schema_version: int = 1
    demo: bool = False

    def __post_init__(self):
        if (not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", self.code)
                or self.level not in LEVELS or self.impact not in IMPACTS
                or self.presentation not in PRESENTATIONS):
            raise ValueError("Invalid event classification")
        if self.schema_version != 1 or type(self.demo) is not bool:
            raise ValueError("Invalid event schema")
        if not isinstance(self.event_id, str):
            raise ValueError("Invalid event id")
        object.__setattr__(self, "event_id", str(uuid.UUID(self.event_id)))
        if self.session_id is not None:
            if not isinstance(self.session_id, str):
                raise ValueError("Invalid session id")
            object.__setattr__(self, "session_id", str(uuid.UUID(self.session_id)))
        occurred = datetime.fromisoformat(self.occurred_at)
        if occurred.tzinfo is None:
            raise ValueError("Event timestamp must include a timezone")
        object.__setattr__(self, "occurred_at", occurred.isoformat(timespec="milliseconds"))
        for key, length in (("message", 500), ("source", 80), ("target", 200), ("dedupe_key", 150)):
            value = getattr(self, key)
            if value is not None or key in ("message", "source"):
                object.__setattr__(self, key, safe_text(value, length))
        object.__setattr__(self, "details", safe_details(self.details))

    def record(self):
        return {key: dict(value) if key == "details" else value
                for key, value in self.__dict__.items()}


class JsonlStore:
    """One family per process/run; never overwrite another application's logs."""
    def __init__(self, directory: Path, *, max_bytes=2 * 1024 * 1024, backups=2, keep_runs=5):
        self.directory = Path(directory)
        self.max_bytes, self.backups, self.keep_runs = max_bytes, backups, keep_runs
        self.stem = "events-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.path = self.directory / (self.stem + ".jsonl")
        self.file = None
        self.closed = False
        self.lock = threading.RLock()

    def _prune(self):
        families = sorted(self.directory.glob("events-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in families[self.keep_runs:]:
            if path == self.path:
                continue
            try:
                # An open family cannot be removed on Windows; leave active runs.
                path.unlink()
                for old in self.directory.glob(path.name + ".*"):
                    old.unlink()
            except OSError:
                pass

    def append(self, event: Event):
        data = json.dumps(event.record(), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self.lock:
            if self.closed:
                return False
            try:
                if self.file is None:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    if self.path.exists() and self.path.stat().st_size:
                        with self.path.open("rb") as existing:
                            existing.seek(-1, 2)
                            torn = existing.read(1) != b"\n"
                    else:
                        torn = False
                    self.file = self.path.open("ab")
                    if torn:
                        self.file.write(b"\n")
                    self._prune()
                if self.file.tell() and self.file.tell() + len(data) > self.max_bytes:
                    self.file.close()
                    self.file = None
                    for index in range(self.backups, 0, -1):
                        old = self.path if index == 1 else Path(str(self.path) + f".{index - 1}")
                        new = Path(str(self.path) + f".{index}")
                        if old.exists():
                            old.replace(new)
                    self.file = self.path.open("ab")
                self.file.write(data)
                self.file.flush()
                return True
            except OSError:
                if self.file is not None:
                    try:
                        self.file.close()
                    except OSError:
                        pass
                    self.file = None
                return False

    def read(self, *, limit=2000):
        """Bounded historical read; corrupt/partial lines never become notices."""
        rows = deque(maxlen=limit)
        with self.lock:
            if not self.directory.is_dir():
                return []
            families = {}
            for path in self.directory.glob("events-*.jsonl*"):
                match = re.fullmatch(r"(events-.+\.jsonl)(?:\.(\d+))?", path.name)
                if match is None:
                    continue
                try:
                    modified = path.stat().st_mtime_ns
                except OSError:
                    continue
                family = families.setdefault(match[1], {"modified": 0, "files": []})
                family["modified"] = max(family["modified"], modified)
                family["files"].append((int(match[2] or 0), path))
            # Windows timestamp updates can lag, and rotations can share the
            # same mtime. The suffix defines order within one run: .2, .1, live.
            files = [path for _, family in sorted(families.items(), key=lambda pair: (pair[1]["modified"], pair[0]))
                     for _, path in sorted(family["files"], key=lambda pair: -pair[0])]
            for path in files:
                try:
                    with path.open("rb") as source:
                        for raw in source:
                            if len(raw) > 16384:
                                continue
                            try:
                                record = json.loads(raw)
                                event = Event(**record)
                                rows.append(event.record())
                            except (ValueError, TypeError, UnicodeError):
                                continue
                except OSError:
                    continue
        return list(rows)

    def export(self, path):
        rows = self.read()
        with Path(path).open("w", encoding="utf-8") as output:
            json.dump({"schema_version": 1, "events": rows}, output, ensure_ascii=False, indent=2)
        return len(rows)

    def close(self):
        with self.lock:
            self.closed = True
            if self.file is not None:
                try:
                    self.file.close()
                except OSError:
                    pass
                self.file = None


class EventCenter:
    """Single entry for disk, a bounded GUI inbox and sticky per-scope notices."""
    def __init__(self, directory, **store_options):
        self.store = JsonlStore(directory, **store_options)
        self.lock = threading.RLock()
        self.inbox = deque(maxlen=2000)
        self.history = deque(maxlen=2000)
        self.notices = {}
        self.strong = deque(maxlen=20)
        self.seen = set()
        self.disk_failed = False
        self.closed = False

    def _publish(self, event, scope, incident):
        self.inbox.append(event)
        self.history.append(event)
        previous = self.notices.get(scope)
        if event.presentation in ("banner", "strong"):
            self.notices[scope] = event
        if event.presentation == "strong":
            key = (scope, event.session_id, incident, event.dedupe_key or event.code)
            if key not in self.seen:
                self.seen.add(key)
                self.strong.append(event)
            elif previous in self.strong:
                # Two failures arriving before the next GUI poll must still
                # produce one reminder showing the most recent information.
                self.strong = deque((event if e == previous else e for e in self.strong), maxlen=20)

    def emit(self, code, message, *, scope="operation", incident=0, **fields):
        event = Event(code, message, **fields)
        with self.lock:
            if self.closed:
                return event
            saved = self.store.append(event)
            self._publish(event, scope, incident)
            if not saved and not self.disk_failed:
                self.disk_failed = True
                self._publish(Event("log.write_failed", "日志无法写入磁盘；本次事件仍在内存，请检查数据目录。",
                                    source="log", level="WARN", presentation="banner"), "log", 0)
            elif saved and self.disk_failed:
                self.disk_failed = False
                self.notices.pop("log", None)
                recovered = Event("log.write_recovered", "日志写入已恢复。", source="log")
                self.store.append(recovered)
                self._publish(recovered, "log", 0)
        return event

    def clear(self, scope):
        with self.lock:
            self.notices.pop(scope, None)
            self.strong = deque((e for e in self.strong if e in self.notices.values()), maxlen=20)

    def reset_dedupe(self, scope):
        with self.lock:
            self.seen = {key for key in self.seen if key[0] != scope}

    def drain(self):
        with self.lock:
            rows, popups = list(self.inbox), list(self.strong)
            self.inbox.clear()
            self.strong.clear()
            return rows, popups

    def notice(self):
        with self.lock:
            return max(self.notices.values(), default=None,
                       key=lambda e: (IMPACTS.index(e.impact), LEVELS.index(e.level), e.occurred_at))

    def notice_events(self):
        with self.lock:
            return tuple(self.notices.values())

    def notice_for(self, scope):
        with self.lock:
            return self.notices.get(scope)

    def export(self, path):
        with self.lock:
            memory = [event.record() for event in self.history]
        rows = {record["event_id"]: record for record in self.store.read()}
        rows.update((record["event_id"], record) for record in memory)
        records = sorted(rows.values(), key=lambda r: r["occurred_at"])[-2000:]
        with Path(path).open("w", encoding="utf-8") as output:
            json.dump({"schema_version": 1, "events": records}, output, ensure_ascii=False, indent=2)
        return len(records)

    def close(self):
        with self.lock:
            self.closed = True
            self.store.close()
