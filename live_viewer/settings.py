from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PLATFORMS = {"B 站": "bilibili", "斗鱼": "douyu"}
DEFAULT_ROOMS = [
    {"platform": "bilibili", "room": "35", "name": "B 站 · 35"},
    {"platform": "douyu", "room": "6657", "name": "斗鱼 · 6657"},
    {"platform": "douyu", "room": "36252", "name": "斗鱼 · 36252"},
]


def data_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / ".local"


def atomic_write(path: Path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def room_record(value) -> dict | None:
    if not isinstance(value, dict) or value.get("platform") not in PLATFORMS.values():
        return None
    room = str(value.get("room", ""))
    if not room.isascii() or not room.isdigit() or not 1 <= len(room) <= 15 or int(room) <= 0:
        return None
    platform = value["platform"]
    name = value.get("name")
    name = name.strip()[:80] if isinstance(name, str) else ""
    return {"platform": platform, "room": str(int(room)), "name": name or f"{next(k for k, v in PLATFORMS.items() if v == platform)} · {int(room)}"}


def sanitize_settings(data) -> dict:
    if not isinstance(data, dict):
        data = {}
    result = {"version": 2, "favorites": [], "history": [], "player": "", "last_room": {},
              "room_preferences": {}, "remember_login": {v: False for v in PLATFORMS.values()},
              "low_latency": data.get("low_latency") is not False}
    result["playback_backend"] = "mpv" if data.get("playback_backend") == "mpv" else "potplayer"
    result["auto_reconnect"] = data.get("auto_reconnect") is not False
    result["native_replay"] = data.get("native_replay") is not False
    minutes = data.get("replay_minutes", 300)
    result["replay_minutes"] = min(300, max(10, minutes)) if type(minutes) is int else 300
    result["vsr"] = data.get("vsr") is True
    volume = data.get("volume", 60)
    result["volume"] = min(100, max(0, float(volume))) if type(volume) in (int, float) else 60
    result["mute"] = data.get("mute") is True
    result["network_mode"] = "system" if data.get("network_mode") == "system" else "direct"
    ui = data.get("ui", {})
    result["ui"] = {"strong_notifications": not isinstance(ui, dict) or ui.get("strong_notifications") is not False,
                    "background_notifications": not isinstance(ui, dict) or ui.get("background_notifications") is not False}
    for key, limit in (("favorites", 500), ("history", 30)):
        items = data.get(key, DEFAULT_ROOMS if key == "favorites" else [])
        if not isinstance(items, list):
            items = DEFAULT_ROOMS if key == "favorites" else []
        seen = set()
        for value in items:
            record = room_record(value)
            if record is None or (record["platform"], record["room"]) in seen:
                continue
            seen.add((record["platform"], record["room"]))
            if key == "history":
                stamp = value.get("played_at")
                record["played_at"] = stamp[:40] if isinstance(stamp, str) else ""
            result[key].append(record)
            if len(result[key]) >= limit:
                break
    if isinstance(data.get("player"), str):
        result["player"] = data["player"][:4096]
    result["last_room"] = room_record(data.get("last_room")) or dict(DEFAULT_ROOMS[0])
    # A room's display name is looked up by platform + numeric ID, never by name.
    names = {(r["platform"], r["room"]): r["name"] for r in result["favorites"]}
    for record in result["history"] + [result["last_room"]]:
        key = (record["platform"], record["room"])
        if key in names:
            record["name"] = names[key]
    preferences = data.get("room_preferences", {})
    if isinstance(preferences, dict):
        for key, value in list(preferences.items())[-1000:]:
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            platform, separator, room = key.partition(":")
            record = room_record({"platform": platform, "room": room}) if separator else None
            if record is None:
                continue
            quality = value.get("quality")
            if quality is not None and (type(quality) is not int or not 0 <= quality <= 100000):
                quality = None
            result["room_preferences"][f"{platform}:{record['room']}"] = {
                "quality": quality, "mode": "导入 Cookie" if value.get("mode") == "导入 Cookie" else "匿名"}
    remembered = data.get("remember_login", {})
    if isinstance(remembered, dict):
        for platform in PLATFORMS.values():
            result["remember_login"][platform] = remembered.get(platform) is True
    for key, limit in (("video_favorites", 500), ("video_history", 30)):
        result[key] = []
        seen = set()
        values = data.get(key, [])
        for value in values[:1000] if isinstance(values, list) else []:
            record = video_record(value)
            if record is None or (record["bvid"], record["cid"]) in seen:
                continue
            seen.add((record["bvid"], record["cid"]))
            if key == "video_history":
                stamp = value.get("played_at", "")
                record["played_at"] = stamp[:40] if isinstance(stamp, str) else ""
            result[key].append(record)
            if len(result[key]) >= limit:
                break
    result["last_video"] = video_record(data.get("last_video"))
    from .bangumi_state import records as pgc_records, record as pgc_record
    result["bangumi_favorites"] = pgc_records(data.get("bangumi_favorites"))
    result["bangumi_history"] = pgc_records(data.get("bangumi_history"), episode=True)
    result["last_bangumi"] = pgc_record(data.get("last_bangumi"), episode=True)
    names = {r["season_id"]: r["name"] for r in result["bangumi_favorites"]}
    for item in result["bangumi_history"] + ([result["last_bangumi"]] if result["last_bangumi"] else []):
        item["name"] = names.get(item["season_id"], item["name"])
    from .vod_state import progress_records
    result["video_progress"] = progress_records(data.get("video_progress"))
    result["vod_resume"] = data.get("vod_resume") is not False
    return result


def video_record(value):
    if not isinstance(value, dict) or not isinstance(value.get("bvid"), str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", value["bvid"]):
        return None
    if any(type(value.get(k)) is not int or value[k] <= 0 for k in ("cid", "page")) or value["page"] > 10000:
        return None
    name = value.get("name", "")
    return {"bvid": value["bvid"], "cid": value["cid"], "page": value["page"],
            "name": name.strip()[:80] if isinstance(name, str) and name.strip() else value["bvid"] + f" · P{value['page']}"}


def remember_video(history, value):
    record = video_record(value)
    if record is None:
        return history
    record["played_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return [record] + [item for item in history if (item["bvid"], item["cid"]) != (record["bvid"], record["cid"])][:29]


class StateStore:
    def __init__(self, directory: Path | None = None):
        self.directory = (Path(directory) if directory is not None else data_directory()).resolve()
        self.path = self.directory / "settings.json"
        self.warning = ""

    def load(self) -> dict:
        try:
            return sanitize_settings(json.loads(self.path.read_text(encoding="utf-8-sig")))
        except FileNotFoundError:
            return sanitize_settings({})
        except (OSError, ValueError):
            self.warning = "设置文件无法读取，已使用默认设置；原文件会在下次保存前备份。"
            return sanitize_settings({})

    def save(self, data):
        if self.warning and self.path.exists():
            backup = self.directory / f"settings-unreadable-{datetime.now():%Y%m%d-%H%M%S-%f}.bak"
            atomic_write(backup, self.path.read_bytes())
            self.warning = ""
        atomic_write(self.path, json.dumps(sanitize_settings(data), ensure_ascii=False, indent=2).encode("utf-8"))


def remember_play(history: list[dict], favorite: dict) -> list[dict]:
    record = room_record(favorite)
    if record is None:
        return history
    record["played_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return [record] + [item for item in history if (item["platform"], item["room"]) != (record["platform"], record["room"])][:29]
