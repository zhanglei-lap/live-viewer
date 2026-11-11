"""Bounded, credential-free video bookmarks keyed by BV + content ID."""
from datetime import datetime
import math
import re


def progress_key(request):
    if request.get("content_type") == "pgc":
        return f"pgc:{request['ep_id']}:{request['cid']}"
    return request["bvid"] + ":" + str(request["cid"])


def progress_records(values):
    result = {}
    if not isinstance(values, dict):return result
    for key, value in list(values.items())[-500:]:
        if not isinstance(key, str) or not re.fullmatch(r"(?:BV[0-9A-Za-z]{10}|pgc:[1-9][0-9]{0,15}):[1-9][0-9]{0,19}", key) or not isinstance(value, dict):continue
        position, duration = value.get("position"), value.get("duration")
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in (position, duration)):continue
        if not 0 < duration <= 31536000 or not 0 <= position <= duration:continue
        page = value.get("page")
        if type(page) is not int or not 1 <= page <= 10000:continue
        record = {"position": round(position, 3), "duration": round(duration, 3), "page": page,
                  "completed": value.get("completed") is True}
        for field in ("video_key", "audio_key", "subtitle_key"):
            selected = value.get(field)
            record[field] = selected if isinstance(selected, str) and re.fullmatch(r"[A-Za-z0-9.:_-]{1,180}", selected) else None
        stamp = value.get("updated_at", "")
        record["updated_at"] = stamp[:40] if isinstance(stamp, str) else ""
        result[key] = record
    return result


def bookmark(config, request, position, duration, *, completed=False):
    key = progress_key(request)
    record = progress_records({key: {"position": 0 if completed else position, "duration": duration,
        "page": request["page"], "completed": completed, "video_key": request.get("video_key"),
        "audio_key": request.get("audio_key"), "subtitle_key": request.get("subtitle_key"),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}})
    if key not in record:return False
    values = config.setdefault("video_progress", {})
    values.pop(key, None);values.update(record)
    while len(values) > 500:values.pop(next(iter(values)))
    return True


def resume_position(config, bvid, cid):
    record = config.get("video_progress", {}).get(f"{bvid}:{cid}", {})
    if record.get("completed"):return 0
    return record.get("position", 0)
