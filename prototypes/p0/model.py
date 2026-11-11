"""P0 demonstration data and event contract; never imports the production app."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
STATE_LABELS = {
    "idle": "准备好开始观看", "resolving": "正在解析房间",
    "connecting": "正在连接播放器", "playing": "演示播放中",
    "paused": "播放已暂停", "reconnecting": "正在重新连接",
    "stopped": "播放已停止", "unavailable": "房间暂未开播",
    "error": "当前无法播放",
}
PUBLIC_DETAILS = {"host", "stage", "reason", "attempt", "network_mode", "event_count"}


@dataclass
class Room:
    platform: str
    number: str
    name: str

    @property
    def key(self):
        return f"{self.platform}:{self.number}"

    @property
    def caption(self):
        return f"{self.platform} · {self.number}"


@dataclass
class Event:
    level: str
    code: str
    source: str
    message: str
    impact: str = "none"
    presentation: str = "log"
    target: str | None = None
    session_id: str | None = None
    dedupe_key: str | None = None
    details: dict = field(default_factory=dict)
    schema_version: int = 1
    demo: bool = True
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.now(
        timezone(timedelta(hours=8))).isoformat(timespec="milliseconds"))

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError("Unknown event level")
        if self.impact not in ("none", "degraded", "start_blocked", "interrupted", "app_blocked"):
            raise ValueError("Unknown playback impact")
        if self.presentation not in ("log", "toast", "banner", "strong"):
            raise ValueError("Unknown presentation")
        if not set(self.details) <= PUBLIC_DETAILS:
            raise ValueError("Only explicitly permitted public details may be exported")

    def to_dict(self):
        return asdict(self)


class DemoModel:
    def __init__(self):
        self.rooms = [Room("斗鱼", "6657", "玩机器"),
                      Room("B 站", "35", "我的 B 站直播"),
                      Room("B 站", "1883358196", "赛事直播")]
        self.selected = self.rooms[0]
        self.active = None
        self.state = "idle"
        self.session_id = None
        self.events = []
        self.alert = None
        self.acknowledged = set()
        self.logged_in = {"B 站": False, "斗鱼": False}

    def emit(self, level, code, source, message, **kwargs):
        event = Event(level, code, source, message,
                      target=kwargs.pop("target", (self.active or self.selected).key),
                      session_id=self.session_id, **kwargs)
        self.events.append(event)
        if event.presentation in ("banner", "strong"):
            self.alert = event
        return event

    def new_session(self):
        self.session_id = str(uuid4())
        self.active = self.selected
        return self.session_id

    def export(self):
        return {"prototype": "P0", "real_playback": False,
                "contains_credentials": False, "events": [event.to_dict() for event in self.events]}
