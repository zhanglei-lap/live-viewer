"""Cancellable platform checks and explicit, credential-free report schemas."""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path

from .core import PlatformClient, ViewerError, read_flv_sample
from .events import safe_text
from .playback import safe_error
from .settings import atomic_write


def cancelled(event):
    if event is not None and event.is_set():
        raise ViewerError("操作已取消。", code="operation.cancelled", level="DEBUG")


def connection_check(request, cookies=None, *, cancel=None):
    result = {"platform": request["platform"], "room_input": request["room_input"],
              "requested": request["requested"], "network_mode": request["network_mode"],
              "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "stage": "房间 / 播放接口"}
    client = PlatformClient(request["platform"], cookies, network_mode=request["network_mode"])
    try:
        cancelled(cancel)
        if cookies is not None:
            result["stage"] = "登录验证"
            login = client.login_status()
            result["login"] = login
            if login["verified"] is False:
                raise ViewerError(login["message"], code="account.expired", level="WARN")
        result["stage"] = "房间 / 播放接口"
        cancelled(cancel)
        stream = client.resolve(request["room_input"], request["requested"])
        result.update(stage="CDN 直播读取", stream=stream.summary())
        cancelled(cancel)
        result["sample"] = read_flv_sample(client, stream, seconds=2, cancelled=cancel)
        result.update(stage="完成", status="房间接口和 CDN 读取成功；尚未验证播放器画面与声音。")
    except Exception as exc:
        error = safe_error(exc)
        if error.code == "operation.cancelled":
            raise error
        result.update(status=str(error), error_code=error.code, level=error.level)
    finally:
        client.close()
    return clean(result, CONNECTION)


def compare_quality(request, cookies=None, *, cancel=None):
    rows, available = [], {}
    for label, jar in (("匿名", None), ("已导入 Cookie", cookies)):
        cancelled(cancel)
        if label != "匿名" and jar is None:
            rows.append({"session": label, "status": "未登录，账号对比尚未执行", "level": "INFO", "skipped": True})
            continue
        row = {"session": label, "tested_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        client = PlatformClient(request["platform"], jar, network_mode=request["network_mode"])
        try:
            if jar is not None:
                row["login"] = client.login_status()
                if row["login"]["verified"] is False:
                    raise ViewerError(row["login"]["message"], code="account.expired", level="WARN")
            cancelled(cancel)
            stream = client.resolve(request["room_input"], request["requested"])
            row.update(stream.summary())
            available.update((q.value, q) for q in stream.qualities)
            cancelled(cancel)
            row["sample"] = read_flv_sample(client, stream, seconds=6, cancelled=cancel)
            row["status"] = "流读取成功；画面、声音与延迟需实播确认"
            row["level"] = "INFO"
        except Exception as exc:
            error = safe_error(exc)
            if error.code == "operation.cancelled":
                raise error
            row.update(status=str(error), error_code=error.code, level=error.level)
        finally:
            client.close()
        rows.append(clean(row, COMPARISON_ROW))
    cancelled(cancel)
    return rows, sorted(available.values(), key=lambda q: q.value, reverse=True)


def scalar(value):
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return safe_text(value)
    return None


def shape(*keys, **nested):
    return {**dict.fromkeys(keys), **nested}


TRACK = shape("key", "kind", "quality", "label", "codec", "bandwidth", "width", "height", "fps")
EPISODE = shape("ep_id", "season_id", "cid", "aid", "bvid", "number", "title", "duration", "section", "status", "badge")
STREAM = shape("platform", "room", "requested", "actual", "actual_name", "different_from_request",
               "nominal_kbps", "format", "codec_requested", "cdn_candidates", "kind", "bvid", "cid", "page", "title", "duration_seconds",
               "content_type", "season_title", "media_duration_seconds", "play_detail",
               episode=EPISODE, account=shape("login", "vip", "pgc_login", "pgc_vip", "network_mode", "trust_env"),
               video=TRACK, audio=TRACK, subtitle=shape("key", "language", "name"))
LOGIN = shape("verified", "message")
SAMPLE = shape("verification", "decoded", "video_codec", "video_tags", "audio_tags", "bytes_read",
               "media_seconds", "sample_total_kbps",
               metadata=shape("width", "height", "framerate", "videodatarate", "audiodatarate"))
CONNECTION = shape("platform", "room_input", "requested", "network_mode", "checked_at", "stage", "status",
                   "error_code", "level", stream=STREAM, login=LOGIN, sample=SAMPLE)
COMPARISON_ROW = {**STREAM, **shape("session", "status", "tested_at", "error_code", "level", "skipped",
                                   login=LOGIN, sample=SAMPLE)}
TIMING = shape("received_media_seconds", "forwarded_media_seconds", "forwarded_clock_drift_seconds",
               "stream_progress_gap_seconds", "max_write_seconds", "player_display_latency_measured",
               "pending_bytes", "pending_seconds", "peak_pending_bytes", "peak_pending_seconds")
TRANSPORT = shape("status", "bytes_forwarded", "connections", "failures", "elapsed_seconds", "kind", "url_refreshes",
                  last_stream=STREAM, timing=TIMING)
PLAYER = shape("backend", "state", "raw_state", "position_seconds", "width", "height", "fps", "volume",
               "mute", "owned_source", "source_present", "decoded_verified", "visible_dialogs", "version",
               "duration_seconds", "replay_enabled", "replay_minutes", "vsr_requested", "private_memory_mb", "working_set_mb")
PLAYBACK = shape("platform", "room_input", "room_name", "requested", "session", "backend", "network_mode", "kind", "bvid", "cid", "page",
                 "low_latency", "state", "message", "session_id", "target", "decoded_verified", "resume_position", "resume_paused", "subtitle_key",
                 "picture_and_audio_confirmed_by_user", "user_note", "auto_reconnect_attempts", "replay_mode", "replay_minutes", "vsr", "content_type", "ep_id", "season_id",
                 transport=TRANSPORT, potplayer=PLAYER)
REPORT = shape("schema_version", "generated_at", "app_version", playback=PLAYBACK,
               connection=CONNECTION, comparison=[COMPARISON_ROW],
               comparison_request=shape("platform", "room_input", "requested", "network_mode", "room_name"))


def clean(value, schema):
    if isinstance(schema, list):
        return [clean(item, schema[0]) for item in value[:100] if isinstance(item, dict)] if isinstance(value, list) else []
    if not isinstance(value, dict):
        return {}
    return {key: clean(value[key], child) if child is not None else scalar(value[key])
            for key, child in schema.items() if key in value}


def save_diagnostic_report(path: Path, report):
    safe = clean(report, REPORT)
    atomic_write(Path(path), json.dumps(safe, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
