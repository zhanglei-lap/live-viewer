from __future__ import annotations

import ctypes as C
import math
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from .core import ViewerError


class Event(C.Structure):
    _fields_ = [("id", C.c_int), ("error", C.c_int), ("userdata", C.c_uint64), ("data", C.c_void_p)]


class EventProperty(C.Structure):
    _fields_ = [("name", C.c_char_p), ("format", C.c_int), ("data", C.c_void_p)]


class EndFile(C.Structure):
    _fields_ = [("reason", C.c_int), ("error", C.c_int), ("entry_id", C.c_int64),
                ("insert_id", C.c_int64), ("insert_count", C.c_int)]


class ClientMessage(C.Structure):
    _fields_ = [("num_args", C.c_int), ("args", C.POINTER(C.c_char_p))]


OBSERVED = {
    "pause": 3, "mute": 3, "paused-for-cache": 3, "idle-active": 3,
    "vo-configured": 3, "eof-reached": 3, "fullscreen": 3,
    "video-params/w": 4, "video-params/h": 4, "playlist-count": 4,
    "decoder-frame-drop-count": 4, "frame-drop-count": 4,
    "audio-params/samplerate": 4,
    "time-pos": 5, "volume": 5, "container-fps": 5, "demuxer-cache-duration": 5,
    "video-codec": 1, "audio-codec": 1, "hwdec-current": 1,
}


def library_path() -> Path:
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    candidates = [base / "runtime" / "mpv" / "libmpv-2.dll"]
    if not getattr(sys, "frozen", False):
        candidates.append(base / "vendor" / "libmpv" / "libmpv-2.dll")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ViewerError("未找到内置播放引擎，请完整解压新版便携包，或选择 PotPlayer。")


def live_controls_path() -> Path:
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    return base / "runtime" / "mpv" / "live-controls.lua" if getattr(sys, "frozen", False) else base / "live_viewer" / "assets" / "live-controls.lua"


def validate_local_stream(url):
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                 and parsed.port is not None and 0 < parsed.port <= 65535
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                 and re.fullmatch(r"/[A-Za-z0-9_-]+/live\.flv", parsed.path))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ViewerError("内置播放器仅接收本工具生成的本机直播地址。")


def property_value(prop):
    if not prop.data or prop.format == 0:
        return None
    types = {3: C.c_int, 4: C.c_int64, 5: C.c_double, 1: C.c_char_p}
    if prop.format not in types:
        return None
    value = C.cast(prop.data, C.POINTER(types[prop.format])).contents.value
    if prop.format == 1:
        return value.decode("utf-8", errors="replace")[:160] if value else None
    if prop.format == 3:
        return bool(value)
    if prop.format == 5 and not math.isfinite(value):
        return None
    return value


class MpvPlayer:
    """Small typed binding to libmpv; UI calls are non-blocking commands/events."""

    def __init__(self, hwnd: int | None = None, *, volume=60, mute=False, library=None):
        self.closed = False
        self.handle = None
        self.props = {}
        self.state = "idle"
        self.entry_id = None
        self.ignored_entries = set()
        self.load_serial = 0
        self.command_serial = 1000
        self.load_request_id = None
        self.pending_stop_id = None
        self.pending_url = None
        self.last_error = None
        self._dll_directory = None
        try:
            path = Path(library) if library is not None else library_path()
            if os.name == "nt":
                self._dll_directory = os.add_dll_directory(str(path.parent))
            self.lib = C.CDLL(str(path))
            self._bind()
            version = self.lib.mpv_client_api_version()
            if version >> 16 != 2:
                raise ViewerError("内置播放引擎 API 版本不兼容，请使用随本程序提供的引擎。")
            self.handle = self.lib.mpv_create()
            if not self.handle:
                raise ViewerError("无法创建内置播放器。")
            options = {"config": "no", "terminal": "no",
                       "msg-level": "all=no", "load-scripts": "no", "idle": "yes", "ytdl": "no", "osc": "no",
                       "input-default-bindings": "no", "input-vo-keyboard": "no",
                       "vo": "gpu", "gpu-api": "d3d11", "hwdec": "auto-safe",
                       "cache": "yes", "cache-secs": "2", "demuxer-max-bytes": "16MiB",
                       "demuxer-max-back-bytes": "32MiB", "demuxer-seekable-cache": "yes",
                       # All player I/O is loopback. Use FFmpeg's explicit proxy
                       # override; the pinned build's curl backend inherits env.
                       "curl-enabled": "no",
                       # FFmpeg treats a non-HTTP proxy string as explicit direct.
                       # An empty mpv option is omitted and still inherits http_proxy.
                       "stream-lavf-o": "http_proxy=none",
                       "cache-on-disk": "no", "cache-pause-wait": "0.5", "network-timeout": "60",
                       "volume": str(volume), "volume-max": "100", "mute": "yes" if mute else "no",
                       "load-auto-profiles": "no"}
            if hwnd is not None:
                options["wid"] = str(hwnd & 0xFFFFFFFF)
            else:
                controls = live_controls_path()
                if not controls.is_file():
                    raise ViewerError("未找到直播控制条，请完整解压新版便携包。")
                options.update({"osc": "yes", "input-default-bindings": "yes", "input-vo-keyboard": "yes",
                                "input-cursor": "yes", "force-window": "yes", "geometry": "1024x576",
                                "title": "${media-title} · 本地直播", "force-media-title": "本地直播",
                                "script-opts": ",".join([
                                    "osc-layout=slimbottombar", "osc-hidetimeout=2000", "osc-visibility=auto",
                                    "osc-title_mbtn_mid_command=", "osc-idlescreen=no",
                                    "osc-menu_mbtn_left_command=script-message-to live_controls viewer-help",
                                    "osc-custom_button_1_content=⏯", "osc-custom_button_1_mbtn_left_command=cycle pause",
                                    "osc-custom_button_2_content=−", "osc-custom_button_2_mbtn_left_command=osd-msg add volume -5",
                                    "osc-custom_button_3_content=♪", "osc-custom_button_3_mbtn_left_command=osd-msg cycle mute",
                                    "osc-custom_button_4_content=+", "osc-custom_button_4_mbtn_left_command=osd-msg add volume 5"]),
                                "scripts": str(controls), "load-console": "no", "load-commands": "no",
                                "load-select": "no", "load-context-menu": "no", "osd-font": "Microsoft YaHei UI"})
            for name, value in options.items():
                if self.lib.mpv_set_option_string(self.handle, name.encode(), value.encode()) < 0:
                    raise ViewerError(f"内置播放引擎不支持所需选项：{name}。")
            if self.lib.mpv_initialize(self.handle) < 0:
                raise ViewerError("内置播放器初始化失败，请检查显卡驱动或选择 PotPlayer。")
            for index, (name, fmt) in enumerate(OBSERVED.items(), 1):
                self.lib.mpv_observe_property(self.handle, index, name.encode(), fmt)
        except (OSError, AttributeError):
            self.close()
            raise ViewerError("内置播放引擎无法加载，请使用完整的 Windows x64 便携包。") from None
        except Exception:
            self.close()
            raise

    def _bind(self):
        declarations = {
            "mpv_client_api_version": ([], C.c_ulong), "mpv_create": ([], C.c_void_p),
            "mpv_set_option_string": ([C.c_void_p, C.c_char_p, C.c_char_p], C.c_int),
            "mpv_initialize": ([C.c_void_p], C.c_int),
            "mpv_observe_property": ([C.c_void_p, C.c_uint64, C.c_char_p, C.c_int], C.c_int),
            "mpv_command_async": ([C.c_void_p, C.c_uint64, C.POINTER(C.c_char_p)], C.c_int),
            "mpv_set_property_async": ([C.c_void_p, C.c_uint64, C.c_char_p, C.c_int, C.c_void_p], C.c_int),
            "mpv_set_property_string": ([C.c_void_p, C.c_char_p, C.c_char_p], C.c_int),
            "mpv_get_property_async": ([C.c_void_p, C.c_uint64, C.c_char_p, C.c_int], C.c_int),
            "mpv_wait_event": ([C.c_void_p, C.c_double], C.POINTER(Event)),
            "mpv_terminate_destroy": ([C.c_void_p], None),
        }
        for name, (args, result) in declarations.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result

    def command(self, *arguments):
        if self.closed or not self.handle:
            return
        self.command_serial += 1
        args = (C.c_char_p * (len(arguments) + 1))(*[str(arg).encode("utf-8") for arg in arguments], None)
        if self.lib.mpv_command_async(self.handle, self.command_serial, args) < 0:
            raise ViewerError("内置播放器无法执行操作，请重新连接。")
        return self.command_serial

    def load(self, url):
        validate_local_stream(url)
        self.poll()
        if self.entry_id is not None:
            self.ignored_entries.add(self.entry_id)
        self.entry_id = None
        self.load_serial += 1
        self.state, self.last_error = "loading", None
        self.props = {k: v for k, v in self.props.items() if k in ("volume", "mute")}
        self.pending_url = url
        if self.pending_stop_id is None:
            self._submit_load()

    def _submit_load(self):
        if self.pending_url is not None:
            # Replace, never append. Pause resets atomically with this new file.
            self.load_request_id = self.command("loadfile", self.pending_url, "replace", "-1", "pause=no")
            self.pending_url = None

    def set_property(self, name, value):
        if self.closed or not self.handle:
            return
        fmt = OBSERVED[name]
        native = C.c_int(int(bool(value))) if fmt == 3 else C.c_double(float(value))
        self.command_serial += 1
        self.lib.mpv_set_property_async(self.handle, self.command_serial, name.encode(), fmt, C.byref(native))

    def set_title(self, title):
        if not self.closed and self.handle:
            self.lib.mpv_set_property_string(self.handle, b"force-media-title", title.encode("utf-8"))

    def set_status(self, text):
        self.command("script-message-to", "live_controls", "viewer-status", text)

    def stop(self):
        self.state = "stopped"
        if self.entry_id is not None:
            self.ignored_entries.add(self.entry_id)
        self.entry_id = None
        self.pending_url = None
        if self.pending_stop_id is None:
            self.pending_stop_id = self.command("stop")

    def poll(self):
        events = []
        if self.closed or not self.handle:
            return events
        for _ in range(256):
            event = self.lib.mpv_wait_event(self.handle, 0).contents
            if event.id == 0:
                break
            if event.id == 5 and event.userdata == self.pending_stop_id:
                self.pending_stop_id = None
                self._submit_load()
            elif event.id in (22, 3) and event.data:
                if event.id == 3 and (event.userdata - 10000) // 100 != self.load_serial:
                    continue
                prop = C.cast(event.data, C.POINTER(EventProperty)).contents
                name = prop.name.decode("utf-8") if prop.name else ""
                if name in OBSERVED:
                    self.props[name] = property_value(prop)
            elif event.id == 6 and event.data:
                entry = C.cast(event.data, C.POINTER(C.c_int64)).contents.value
                if entry not in self.ignored_entries:
                    self.entry_id = entry
            elif event.id in (8, 21) and self.entry_id is not None:
                self.state = "playing"
                for index, (name, fmt) in enumerate(OBSERVED.items(), 1):
                    self.lib.mpv_get_property_async(self.handle, 10000 + self.load_serial * 100 + index, name.encode(), fmt)
                events.append({"kind": "ready", "load_serial": self.load_serial})
            elif event.id == 7 and event.data:
                data = C.cast(event.data, C.POINTER(EndFile)).contents
                if data.entry_id == self.entry_id and data.entry_id not in self.ignored_entries:
                    self.state = "ended" if data.reason == 0 else "error"
                    self.last_error = int(data.error)
                    events.append({"kind": "ended", "reason": int(data.reason), "error": int(data.error),
                                   "load_serial": self.load_serial})
            elif event.id == 5 and event.error < 0 and self.state == "loading" and event.userdata == self.load_request_id:
                self.state, self.last_error = "error", int(event.error)
                events.append({"kind": "ended", "reason": 4, "error": int(event.error), "load_serial": self.load_serial})
            elif event.id == 1:
                self.state = "closed"
                events.append({"kind": "closed"})
            elif event.id == 16 and event.data:
                message = C.cast(event.data, C.POINTER(ClientMessage)).contents
                if 1 <= message.num_args <= 4:
                    args = [message.args[i].decode("utf-8", errors="replace") for i in range(message.num_args)]
                    if args == ["viewer-return-live"]:
                        events.append({"kind": "reconnect"})
        return events

    def snapshot(self):
        width, height = self.props.get("video-params/w"), self.props.get("video-params/h")
        return {"backend": "libmpv", "state": self.state, "load_serial": self.load_serial,
                "width": width, "height": height, "fps": self.props.get("container-fps"),
                "video_codec": self.props.get("video-codec"), "audio_codec": self.props.get("audio-codec"),
                "audio_samplerate": self.props.get("audio-params/samplerate"),
                "hardware_decoder": self.props.get("hwdec-current"),
                "position_seconds": self.props.get("time-pos"),
                "fullscreen": bool(self.props.get("fullscreen")),
                "cache_seconds": self.props.get("demuxer-cache-duration"),
                "paused": bool(self.props.get("pause")), "buffering": bool(self.props.get("paused-for-cache")),
                "playlist_count": self.props.get("playlist-count"),
                "dropped_frames": self.props.get("decoder-frame-drop-count"),
                "video_output_ready": bool(self.state == "playing" and width and height and self.props.get("vo-configured")),
                "error_code": self.last_error}

    def close(self):
        if self.closed:
            return
        self.closed = True
        handle, self.handle = self.handle, None
        if handle is not None:
            self.lib.mpv_terminate_destroy(handle)
        if self._dll_directory is not None:
            self._dll_directory.close()
            self._dll_directory = None
