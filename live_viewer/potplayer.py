"""Owned PotPlayer process, private portable profile and bounded Win32 control."""
from __future__ import annotations

import configparser
import ctypes
from ctypes import wintypes as W
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse

from .core import ViewerError
from .settings import atomic_write
from .resources import CacheLease, process_memory

WM_USER, WM_COMMAND, WM_COPYDATA, WM_CLOSE = 0x400, 0x111, 0x4A, 0x10
GET_VOLUME, SET_VOLUME, GET_POSITION, GET_STATE, GET_MUTE = 0x5000, 0x5001, 0x5004, 0x5006, 0x5011
SET_STATE, CLOSE_MEDIA, GET_FILENAME = 0x5007, 0x5009, 0x6020
GET_WIDTH, GET_HEIGHT, GET_FPS, SHOW_MESSAGE = 0x6030, 0x6031, 0x6032, 0x6040
PLAY_PAUSE = 10014
STATE_NAMES = {-1: "closed_media", 0: "ended", 1: "paused", 2: "playing"}


def file_version(path):
    if os.name != "nt":
        return None
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [W.LPCWSTR, ctypes.POINTER(W.DWORD)]
    api.GetFileVersionInfoSizeW.restype = W.DWORD
    api.GetFileVersionInfoW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p]
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(W.UINT)]
    ignored = W.DWORD()
    size = api.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    pointer, length = ctypes.c_void_p(), W.UINT()
    if not api.GetFileVersionInfoW(str(path), 0, size, data) or not api.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    values = ctypes.cast(pointer, ctypes.POINTER(W.DWORD))
    if length.value < 16 or values[0] != 0xFEEF04BD:
        return None
    return ".".join(map(str, (values[2] >> 16, values[2] & 65535, values[3] >> 16, values[3] & 65535)))


def valid_local_url(value):
    parsed = urlparse(value)
    if (parsed.scheme not in ("http", "rtmp") or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password
            or not parsed.port or parsed.query or parsed.fragment or not (parsed.path.endswith("/live.flv") or
                parsed.scheme == "http" and parsed.path.endswith(("/vod-video.mp4", "/vod-audio.m4a")))):
        raise ViewerError("播放器只接受本工具提供的本机媒体地址。", code="player.source_invalid")
    return value


def read_ini(path):
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str
    try:
        raw = Path(path).read_bytes()
        text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig")
        parser.read_string(text)
    except (OSError, UnicodeError, configparser.Error):
        pass
    return parser


def write_ini(path, parser):
    import io
    output = io.StringIO()
    parser.write(output, space_around_delimiters=False)
    atomic_write(Path(path), output.getvalue().encode("utf-16"))


class PrivateProfile:
    """Copy static installed files once; each run has its own writable INI."""
    def __init__(self, data_directory, executable):
        self.root = (Path(data_directory) / "potplayer").resolve()
        self.source = Path(executable).resolve()
        self.path = None
        self.executable = None
        self.ini = None
        self.lock = threading.Lock()
        self.engine_id = None
        self.version = None
        self.closed = False

    def prepare(self):
        with self.lock:
            if self.closed:
                raise ViewerError("播放器准备已取消。", code="playback.cancelled", level="DEBUG")
            if self.executable is not None:
                return
            if not self.source.is_file() or self.source.suffix.lower() != ".exe":
                raise ViewerError("请选择已安装的 PotPlayer 程序。", code="player.path_invalid")
            try:
                with self.source.open("rb") as source:
                    if source.read(2) != b"MZ":
                        raise ViewerError("所选文件不是 Windows 播放器程序。", code="player.path_invalid")
                    source.seek(0x3C)
                    header = int.from_bytes(source.read(4), "little")
                    source.seek(header)
                    if source.read(6) != b"PE\0\0\x64\x86":
                        raise ViewerError("P2 接管目前支持 64 位 PotPlayer，请选择 PotPlayerMini64.exe。",
                                          code="player.architecture_unsupported")
                library = self.source.parent / "PotPlayer64.dll"
                if not library.is_file():
                    raise ViewerError("播放器目录缺少 PotPlayer64.dll，请选择完整安装目录中的程序。",
                                      code="player.installation_incomplete")
                self.version = file_version(library)
                files = [self.source, *sorted(self.source.parent.glob("*.dll"))]
                for folder in ("Skins", "Language", "Logos", "IconPack", "PxShader"):
                    directory = self.source.parent / folder
                    if directory.is_dir() and not directory.is_junction() and not directory.is_symlink():
                        files.extend(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink())
                for folder in ("FFmpeg62", "Bass64", "OpenCodec"):
                    directory = self.source.parent / "Module" / folder
                    if directory.is_dir() and not directory.is_junction() and not directory.is_symlink():
                        files.extend(p for p in directory.rglob("*.dll") if p.is_file() and not p.is_symlink())
                files = [p for p in files if p.resolve().is_relative_to(self.source.parent)]
                digest = hashlib.sha256()
                for file in files:
                    digest.update(str(file.relative_to(self.source.parent)).encode("utf-8"))
                    stat = file.stat()
                    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
                digest.update(hashlib.sha256(self.source.read_bytes()).digest())
                digest.update(hashlib.sha256(library.read_bytes()).digest())
                self.engine_id = digest.hexdigest()[:24]
                engine = self.root / "engines" / self.engine_id
                marker = engine / "engine.json"
                if not marker.is_file():
                    staging = self.root / "engines" / ("preparing-" + uuid.uuid4().hex)
                    staging.mkdir(parents=True)
                    for file in files:
                        target = staging / file.relative_to(self.source.parent)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file, target)
                    atomic_write(staging / "engine.json", json.dumps({"private_engine": 1, "engine_id": self.engine_id,
                                 "launcher": self.source.name}, ensure_ascii=True).encode())
                    try:
                        staging.rename(engine)
                    except FileExistsError:
                        if not marker.is_file():
                            raise
                        self._remove_owned(staging, self.root / "engines")
                run = self.root / "runs" / uuid.uuid4().hex
                run.mkdir(parents=True)
                atomic_write(run / "owner.json", b'{"private_profile":1}')
                self.path = run
                for file in engine.rglob("*"):
                    if not file.is_file() or file.name == "engine.json":
                        continue
                    relative = file.relative_to(engine)
                    if relative == Path(self.source.name):
                        relative = Path("LiveViewerPot.exe")
                    target = run / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(file, target)  # Links only the application's own cached copies.
                    except OSError:
                        shutil.copy2(file, target)
                self.executable, self.ini = run / "LiveViewerPot.exe", run / "LiveViewerPot.ini"
                parser = read_ini(self.root / "preferences.ini")
                # Never restore stale sources, playlists, histories, or automatic actions.
                for section in list(parser.sections()):
                    if section not in ("Settings", "Window", "Positions"):
                        parser.remove_section(section)
                        continue
                    for key in list(parser[section]):
                        if any(word in key.lower() for word in ("history", "recent", "lastfile", "lastplay", "lastopen", "afterplay", "shutdown")):
                            parser.remove_option(section, key)
                if not parser.has_section("Settings"):
                    parser.add_section("Settings")
                parser["Settings"].update({"AutoUpdateStart": "0", "CheckAutoUpdate": "0", "FastAutoUpdate": "0",
                                           "IgnoreErrorMsg": "1", "PlaybackMode": "0", "LastPlayListName": "LiveViewer",
                                           "MultipleInst": "1"})
                write_ini(self.ini, parser)
            except ViewerError:
                raise
            except OSError:
                raise ViewerError("无法创建 PotPlayer 独立配置，请检查数据目录和播放器安装文件。",
                                  code="player.profile_failed") from None

    @staticmethod
    def _remove_owned(path, parent):
        path, parent = Path(path), Path(parent).resolve()
        resolved = path.resolve()
        if resolved.parent != parent or path.is_symlink() or path.is_junction():
            raise ValueError("Profile cleanup escaped its owned directory")
        if not (path / "owner.json").is_file() and not (path / "engine.json").is_file():
            raise ValueError("Profile cleanup requires an ownership marker")
        shutil.rmtree(resolved)

    def save_and_clean(self):
        with self.lock:
            self.closed = True
            if self.path is None:
                return
            parser = read_ini(self.ini)
            for section in list(parser.sections()):
                if section not in ("Settings", "Window", "Positions"):
                    parser.remove_section(section)
                    continue
                for key in list(parser[section]):
                    if any(word in key.lower() for word in ("history", "recent", "lastfile", "lastplay", "lastopen", "afterplay", "shutdown")):
                        parser.remove_option(section, key)
            # Preferences come only from this application's private player.
            if parser.sections():
                write_ini(self.root / "preferences.ini", parser)
            self._remove_owned(self.path, self.root / "runs")
            self.path = None

    def playlist_summary(self):
        rows = []
        if self.path is None:
            return {"available": False}
        for file in (self.path / "Playlist").glob("*.dpl"):
            try:
                raw = file.read_bytes()
                text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig")
                sources = [line.split("*", 2)[2] for line in text.splitlines()
                           if len(line.split("*", 2)) == 3 and line.split("*", 2)[1] == "file"]
                if sources:
                    rows.append({"items": len(sources), "distinct_sources": len(set(sources)),
                                 "only_loopback": all(urlparse(s).hostname == "127.0.0.1" for s in sources)})
            except (OSError, UnicodeError, ValueError):
                continue
        generated = [p.relative_to(self.path).as_posix() for p in self.path.rglob("*")
                     if p.is_file() and p.suffix.lower() in (".dpl", ".pls", ".m3u", ".asx")]
        return {"available": bool(rows), "albums": rows, "playlist_files": generated}


class Win32Player:
    """Every message is checked against the Popen process and its main window."""
    def __init__(self, process, executable):
        if os.name != "nt":
            raise ViewerError("PotPlayer 接管仅支持 Windows。", code="player.platform_unsupported")
        self.process, self.executable = process, Path(executable).resolve()
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ptr, self.result = ctypes.c_size_t, ctypes.c_ssize_t
        self.enum = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.proc_type = ctypes.WINFUNCTYPE(self.result, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        self.user.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT, ctypes.POINTER(self.ptr)]
        self.user.SendMessageTimeoutW.restype = self.result
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        self.user.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
        self.user.EnumWindows.argtypes = [self.enum, W.LPARAM]
        self.user.IsWindow.argtypes = [W.HWND]
        self.user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.user.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
        self.user.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.user.DefWindowProcW.restype = self.result
        self.kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
        self.kernel.GetModuleHandleW.argtypes = [W.LPCWSTR]
        self.kernel.GetModuleHandleW.restype = W.HMODULE
        self.hwnd = None
        self.filename = None
        self.receiving = False
        self.receiver = None
        self.receiver_proc = None
        self.receiver_class = None
        self.receiver_instance = None

    def owns(self):
        if self.process.poll() is not None:
            return False
        image = ctypes.create_unicode_buffer(32768)
        size = W.DWORD(len(image))
        if not self.kernel.QueryFullProcessImageNameW(W.HANDLE(int(self.process._handle)), 0, image, ctypes.byref(size)):
            return False
        if Path(image.value).resolve() != self.executable:
            return False
        if self.hwnd is not None:
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
            name = ctypes.create_unicode_buffer(256)
            self.user.GetClassNameW(self.hwnd, name, len(name))
            if pid.value != self.process.pid or name.value != "PotPlayer64":
                return False
        return True

    def find(self):
        if not self.owns():
            return False
        found = []
        @self.enum
        def visit(window, _):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == self.process.pid:
                name = ctypes.create_unicode_buffer(256)
                self.user.GetClassNameW(window, name, len(name))
                if name.value == "PotPlayer64":
                    found.append(window)
            return True
        self.user.EnumWindows(visit, 0)
        if found:
            self.hwnd = found[0]
        return self.hwnd is not None and self.owns()

    def send(self, code, value=0, *, message=WM_USER, timeout=200):
        if self.hwnd is None or not self.owns():
            raise ViewerError("PotPlayer 专用窗口已关闭或无法关联，已取消控制。",
                              code="player.ownership_lost", level="WARN")
        result = self.ptr()
        # Do not use SMTO_BLOCK: filename queries reply synchronously to our
        # message-only receiver window; blocking inbound messages would deadlock.
        success = self.user.SendMessageTimeoutW(self.hwnd, message, code, value, 0x22, timeout, ctypes.byref(result))
        if not success:
            raise ViewerError("PotPlayer 控制超时，请关闭其设置或对话框后手动重新连接。",
                              code="player.control_timeout")
        return self.result(result.value).value

    def _make_receiver(self):
        if self.receiver is not None:
            return
        ptr, callback = self.ptr, self.proc_type
        class CopyData(ctypes.Structure):
            _fields_ = [("kind", ptr), ("size", W.DWORD), ("data", ctypes.c_void_p)]
        self.copy_data = CopyData
        @callback
        def receive(hwnd, message, wp, lp):
            if message == WM_COPYDATA:
                if self.receiving and lp:
                    data = ctypes.cast(lp, ctypes.POINTER(CopyData)).contents
                    if data.kind == GET_FILENAME and data.size <= 8192 and data.data:
                        self.filename = ctypes.string_at(data.data, data.size).rstrip(b"\0").decode("utf-8", errors="replace")
                return 1
            return self.user.DefWindowProcW(hwnd, message, wp, lp)
        class WindowClass(ctypes.Structure):
            _fields_ = [("style", W.UINT), ("proc", callback), ("extra_class", ctypes.c_int), ("extra_window", ctypes.c_int),
                        ("instance", W.HINSTANCE), ("icon", W.HICON), ("cursor", W.HANDLE), ("background", W.HBRUSH),
                        ("menu", W.LPCWSTR), ("name", W.LPCWSTR)]
        self.user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        self.user.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, ctypes.c_void_p]
        self.user.CreateWindowExW.restype = W.HWND
        self.receiver_class = "LiveViewerPot" + uuid.uuid4().hex
        self.receiver_instance = self.kernel.GetModuleHandleW(None)
        self.receiver_proc = receive
        klass = WindowClass()
        klass.proc, klass.instance, klass.name = receive, self.receiver_instance, self.receiver_class
        if not self.user.RegisterClassW(ctypes.byref(klass)):
            raise ViewerError("无法创建播放器状态接收窗口。", code="player.receiver_failed")
        self.receiver = self.user.CreateWindowExW(0, self.receiver_class, "", 0, 0, 0, 0, 0,
                                                  W.HWND(-3), None, self.receiver_instance, None)
        if not self.receiver:
            raise ViewerError("无法创建播放器状态接收窗口。", code="player.receiver_failed")

    def snapshot(self, expected_url):
        self._make_receiver()
        self.filename = None
        self.receiving = True
        try:
            self.send(GET_FILENAME, self.receiver)
        finally:
            self.receiving = False
        state = self.send(GET_STATE)
        if state == -1 and self.filename is None:
            self.filename = ""
        if state not in STATE_NAMES or self.filename is None:
            raise ViewerError("当前 PotPlayer 未返回可验证的状态接口，请选择兼容版本。",
                              code="player.protocol_unsupported")
        return {"backend": "potplayer", "state": STATE_NAMES[state], "raw_state": state,
                "position_seconds": self.send(GET_POSITION) / 1000,
                "duration_seconds": self.send(0x5002) / 1000,
                "width": self.send(GET_WIDTH), "height": self.send(GET_HEIGHT),
                "fps": self.send(GET_FPS) / 1000, "volume": self.send(GET_VOLUME),
                "mute": bool(self.send(GET_MUTE)),
                "owned_source": self.filename == expected_url, "source_present": bool(self.filename),
                "decoded_verified": False, "checked_at_monotonic": time.monotonic()}

    def stop(self):
        self.send(CLOSE_MEDIA, timeout=3000)

    def reopen(self, guard=lambda: True):
        # Verified against this version: play button reopens the existing item,
        # keeping its playlist identity and the stable loopback URL.
        self.stop()
        if guard():
            self.send(PLAY_PAUSE, message=WM_COMMAND)
            # Some RTMP sources auto-start while the close/open command is
            # processed. Explicit running state prevents a toggle leaving pause.
            self.send(SET_STATE, 2, timeout=3000)

    def close(self):
        if self.hwnd is not None and self.owns():
            self.user.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def caption(self, title):
        text = ctypes.create_unicode_buffer("本地直播 · " + title[:80])
        self.send(0, ctypes.addressof(text), message=0xC)

    def visible_dialog_count(self):
        count = [0]
        self.user.IsWindowVisible.argtypes = [W.HWND]
        @self.enum
        def visit(window, _):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == self.process.pid and self.user.IsWindowVisible(window):
                name = ctypes.create_unicode_buffer(256)
                self.user.GetClassNameW(window, name, len(name))
                if name.value == "#32770":
                    count[0] += 1
            return True
        self.user.EnumWindows(visit, 0)
        return count[0]

    def inspect_playlist(self):
        self.send(10011, message=WM_COMMAND)
        self.user.EnumChildWindows.argtypes = [W.HWND, self.enum, W.LPARAM]
        data = {"list_counts": [], "classes": []}
        @self.enum
        def visit(window, _):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == self.process.pid:
                name = ctypes.create_unicode_buffer(256)
                self.user.GetClassNameW(window, name, len(name))
                data["classes"].append(name.value.split(":")[0])
                if name.value == "SysListView32":
                    result = self.ptr()
                    if self.user.SendMessageTimeoutW(window, 0x1004, 0, 0, 0x22, 200, ctypes.byref(result)):
                        data["list_counts"].append(result.value)
            return True
        self.user.EnumChildWindows(self.hwnd, visit, 0)
        @self.enum
        def top(window, _):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == self.process.pid and window != self.hwnd:
                self.user.EnumChildWindows(window, visit, 0)
            return True
        self.user.EnumWindows(top, 0)
        self.send(10011, message=WM_COMMAND)
        data["classes"] = sorted(set(data["classes"]))
        return data

    def inspect_playlist(self):
        self.send(10011, message=WM_COMMAND)
        self.user.EnumChildWindows.argtypes = [W.HWND, self.enum, W.LPARAM]
        data = {"list_counts": [], "classes": []}
        @self.enum
        def visit(window, _):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == self.process.pid:
                name = ctypes.create_unicode_buffer(256)
                self.user.GetClassNameW(window, name, len(name))
                data["classes"].append(name.value.split(":")[0])
                if name.value == "SysListView32":
                    result = self.ptr()
                    if self.user.SendMessageTimeoutW(window, 0x1004, 0, 0, 0x22, 200, ctypes.byref(result)):
                        data["list_counts"].append(result.value)
            return True
        self.user.EnumChildWindows(self.hwnd, visit, 0)
        @self.enum
        def top(window, _):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == self.process.pid and window != self.hwnd:
                self.user.EnumChildWindows(window, visit, 0)
            return True
        self.user.EnumWindows(top, 0)
        self.send(10011, message=WM_COMMAND)
        data["classes"] = sorted(set(data["classes"]))
        return data

    def close_receiver(self):
        if self.receiver is not None:
            self.user.DestroyWindow.argtypes = [W.HWND]
            self.user.UnregisterClassW.argtypes = [W.LPCWSTR, W.HINSTANCE]
            self.user.DestroyWindow(self.receiver)
            self.user.UnregisterClassW(self.receiver_class, self.receiver_instance)
            self.receiver = None


class PotPlayerAdapter:
    """Window polling/control runs on one worker; the GUI never waits on RPC."""
    def __init__(self, directory, executable, *, hidden=False):
        self.profile = PrivateProfile(directory, executable)
        self.hidden = hidden
        self.lock = threading.RLock()
        self.messages = queue.Queue()
        self.inbox = queue.SimpleQueue()
        self.process = None
        self.native = None
        self.url = None
        self.intent = "stopped"
        self.serial = 0
        self.ever_launched = False
        self.playlist_after_close = None
        self.playlist_inspection = None
        self.playlist_inspection = None
        self.snapshot_data = {"backend": "potplayer", "state": "idle", "decoded_verified": False}
        self.closed = False
        self.cache = None
        self.options = (False, 300, False)
        self.seeking_until = 0.0
        self.worker = threading.Thread(target=self._run, name="potplayer-control", daemon=True)
        self.worker.start()

    def prepare(self, *, replay=False, replay_minutes=300, vsr=False):
        self.profile.prepare()
        self.options = (bool(replay), min(300, max(10, int(replay_minutes))), bool(vsr))
        parser = read_ini(self.profile.ini)
        parser["Settings"]["UseStreamTimeShift"] = "1" if replay else "0"
        parser["Settings"]["StreamTimeShiftTime"] = str(self.options[1])
        parser["Settings"]["UseD3D11VSR"] = "1" if vsr else "0"
        if not replay:parser["Settings"]["PlayUseKeyFrame"] = "0"
        if vsr:
            parser["Settings"].update(VideoRen2="15", Direct3D11Mode="1")
        write_ini(self.profile.ini, parser)

    def open(self, url, title="本地直播", *, volume=60, mute=False, guard=lambda: True, allow_new_process=True, audio_url=None,
             start_position=0, start_paused=False, subtitle_text=None):
        url = valid_local_url(url)
        if audio_url is not None:
            audio_url = valid_local_url(audio_url)
            if not url.endswith("/vod-video.mp4") or audio_url != url.removesuffix("vod-video.mp4") + "vod-audio.m4a":
                raise ViewerError("音视频必须来自同一个本机点播会话。", code="player.source_invalid")
        if type(start_position) not in (int, float) or not math.isfinite(start_position) or not 0 <= start_position <= 31536000:
            raise ViewerError("播放恢复时间无效。", code="vod.resume_invalid")
        if subtitle_text is not None and (not isinstance(subtitle_text, str) or len(subtitle_text) > 2000000):
            raise ViewerError("字幕内容无效或过大。", code="vod.subtitle_invalid")
        with self.lock:
            if self.closed or not guard():
                raise ViewerError("播放器启动已取消。", code="playback.cancelled", level="DEBUG")
            self.serial += 1
            self.intent, self.url = "loading", url
            self.snapshot_data = {"backend": "potplayer", "state": "loading", "decoded_verified": False,
                                  "load_serial": self.serial}
            self.messages.put(("open", self.serial, (url, title, volume, mute, guard, allow_new_process, audio_url, start_position, bool(start_paused), subtitle_text)))
            return self.serial

    def stop(self):
        with self.lock:
            self.serial += 1
            self.intent = "stopped"
            self.messages.put(("stop", self.serial, None))

    def pause(self):
        with self.lock:
            self.intent = "paused"
            self.messages.put(("pause", self.serial, None))

    def resume(self):
        with self.lock:
            self.messages.put(("user_resume", self.serial, None))

    def seek(self, seconds):
        with self.lock:
            if self.intent == "stopped": return
            self.seeking_until = time.monotonic() + 5
            self.messages.put(("seek", self.serial, max(0, float(seconds))))

    def close_window(self):
        with self.lock:
            self.messages.put(("user_close", self.serial, None))

    def close_media(self):
        with self.lock:
            self.messages.put(("user_stop", self.serial, None))

    def poll(self):
        result = []
        while not self.inbox.empty():
            result.append(self.inbox.get())
        return result

    def snapshot(self):
        with self.lock:
            data = dict(self.snapshot_data)
            if self.process is not None and self.process.poll() is not None:
                data["state"] = "closed"
            data["intent"] = self.intent
            data["version"] = self.profile.version
            data["replay_enabled"], data["replay_minutes"], data["vsr_requested"] = self.options
            return data

    def _emit(self, kind, **fields):
        self.inbox.put({"kind": kind, "serial": self.serial, **fields})

    def _launch(self, args):
        url, title, volume, mute, guard, allow_new_process = args[:6]
        audio_url = args[6] if len(args) > 6 else None
        if not guard():
            return
        self.profile.prepare()
        parser = read_ini(self.profile.ini)
        parser["Settings"]["AudioVolume"] = str(min(100, max(0, int(volume))))
        parser["Settings"]["AudioMute"] = "1" if mute else "0"
        write_ini(self.profile.ini, parser)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0 if self.hidden else 1
        with self.lock:
            if self.closed or self.intent != "loading" or not guard():
                return
            if self.ever_launched and not allow_new_process:
                self._emit("closed")
                self.intent = "stopped"
                return
            self.cache = CacheLease(self.profile.root.parent)
            command = [str(self.profile.executable), "/new", url]
            if audio_url is not None:
                command.append("/aud=" + audio_url)
            if len(args) > 7 and args[7] > 0:
                milli = round(args[7] * 1000);seconds, ms = divmod(milli, 1000);minutes, sec = divmod(seconds, 60);hours, minute = divmod(minutes, 60)
                command.append(f"/seek={hours:02}:{minute:02}:{sec:02}.{ms:03}")
            if len(args) > 9 and args[9]:
                path = self.cache.path / "captions.srt"
                atomic_write(path, args[9].encode("utf-8-sig"))
                command.append("/sub=" + str(path))
            self.process = subprocess.Popen(command,
                                            cwd=self.profile.path, startupinfo=startup,
                                            creationflags=subprocess.CREATE_NO_WINDOW, env=self.cache.environment())
            self.native = Win32Player(self.process, self.profile.executable)
            self.ever_launched = True
        self._emit("launched")

    def _open(self, args):
        url, title, _, _, guard, _ = args[:6]
        if not guard() or self.closed:
            return
        if self.process is None or self.process.poll() is not None:
            self._launch(args)
            return
        if len(args) > 6 and args[6] is not None:
            raise ViewerError("切换点播轨道需要新的专用会话。", code="player.dual_track_restart_required")
        if self.native.hwnd is None and not self.native.find():
            raise ViewerError("专用播放器尚未就绪，请稍后重新连接。", code="player.window_unavailable")
        if self.url != url:
            raise ViewerError("播放器会话地址不匹配，已取消控制。", code="player.source_invalid")
        current = self.native.snapshot(url)
        if current["source_present"] and not current["owned_source"]:
            raise ViewerError("专用播放器已打开其他内容，已取消自动接管。", code="player.source_changed", level="WARN")
        if not guard():
            return
        self.native.reopen(guard)
        if guard():
            self.native.caption(title)
        self._emit("reopen_requested")

    def _run(self):
        opened_at, last_error, opening, query_failed_at = None, None, False, None
        pause_on_start = False
        pause_confirmed_at = None
        start_position = 0
        try:
            while not self.closed:
                try:
                    kind, serial, args = self.messages.get(timeout=.2)
                except queue.Empty:
                    kind, serial, args = None, None, None
                if serial is not None and serial != self.serial:
                    continue
                captured_serial = self.serial
                reading_state = False
                try:
                    if kind == "open":
                        pause_on_start = args[8] if len(args) > 8 else False
                        start_position = args[7] if len(args) > 7 else 0
                        pause_confirmed_at = None
                        self._open(args)
                        opened_at = time.monotonic()
                        opening = True
                    elif kind == "stop" and self.native is not None and self.native.find():
                        self.native.stop()
                    elif kind == "pause" and self.native is not None and self.native.find():
                        self.native.send(SET_STATE, 1)
                    elif kind == "user_resume" and self.native is not None and self.native.find():
                        pause_on_start = False
                        self.native.send(SET_STATE, 2)
                    elif kind == "seek" and self.native is not None and self.native.find():
                        self.native.send(0x5005, int(args * 1000), timeout=3000)
                    elif kind == "user_close" and self.native is not None:
                        self.native.close()
                    elif kind == "user_stop" and self.native is not None and self.native.find():
                        self.native.stop()
                    elif kind == "inspect_playlist" and self.native is not None:
                        self.playlist_inspection = self.native.inspect_playlist()
                    elif kind == "inspect_playlist" and self.native is not None:
                        self.playlist_inspection = self.native.inspect_playlist()
                    if self.process is None:
                        continue
                    if self.process.poll() is not None:
                        with self.lock:
                            self.intent = "stopped"
                            self.snapshot_data = {"backend": "potplayer", "state": "closed", "decoded_verified": False}
                        self._emit("closed", serial=captured_serial)
                        self.native.close_receiver()
                        if self.cache is not None:
                            try:
                                self.cache.close()
                            except (OSError, ValueError):
                                self._emit("cleanup_deferred")
                            self.cache = None
                        self.process, self.native = None, None
                        continue
                    if not self.native.find():
                        if opened_at is not None and time.monotonic() - opened_at > 15:
                            raise ViewerError("未找到本工具创建的 PotPlayer 窗口。", code="player.window_unavailable")
                        continue
                    if self.hidden:
                        self.native.user.ShowWindow(self.native.hwnd, 0)
                    reading_state = True
                    data = self.native.snapshot(self.url)
                    data["visible_dialogs"] = self.native.visible_dialog_count()
                    data.update(process_memory(self.process))
                    data["load_serial"] = captured_serial
                    if time.monotonic() < self.seeking_until and data["state"] in ("closed_media", "ended"):
                        data["state"] = "seeking"
                    if data["state"] in ("playing", "paused") and kind != "seek":
                        self.seeking_until = 0.0
                    if opening and data["state"] in ("closed_media", "ended") and time.monotonic() - opened_at < 5:
                        data["state"] = "loading"
                    if data["state"] in ("playing", "paused"):
                        if pause_on_start and data.get("width", 0) > 0 and data.get("owned_source"):
                            # The command-line seek can resume a graph after the
                            # first pause. Keep the requested pause until the
                            # target and physical paused state have settled.
                            if data["state"] != "paused":
                                self.native.send(SET_STATE, 1)
                                pause_confirmed_at = None
                            elif abs(data.get("position_seconds", 0) - start_position) <= 2:
                                pause_confirmed_at = pause_confirmed_at or time.monotonic()
                                if time.monotonic() - pause_confirmed_at >= 1:
                                    pause_on_start = False
                            else:
                                pause_confirmed_at = None
                            data["state"], data["raw_state"] = "paused", 1
                        opening = False
                    data["opening"] = opening
                    with self.lock:
                        if captured_serial == self.serial:
                            self.snapshot_data = data
                            if data["state"] in ("playing", "paused") and self.intent != "stopped":
                                self.intent = data["state"]
                    last_error = None
                    query_failed_at = None
                except ViewerError as exc:
                    if reading_state and not opening and exc.code == "player.control_timeout":
                        now = time.monotonic()
                        if query_failed_at is None: query_failed_at = now
                        if now - query_failed_at < 3:
                            continue
                    # The HWND can exist while initial media loading is still
                    # blocking its message loop. Retry only state observation,
                    # within the existing five-second loading grace period.
                    # Failures of open/stop/pause commands remain visible.
                    if (reading_state and opening and opened_at is not None
                            and time.monotonic() - opened_at < 5 and exc.code == "player.control_timeout"):
                        continue
                    if last_error != exc.code:
                        self._emit("error", error=exc, serial=captured_serial)
                        last_error = exc.code
                except Exception:
                    error = ViewerError("PotPlayer 操作失败，请检查安装与独立配置。", code="player.operation_failed")
                    if last_error != error.code:
                        self._emit("error", error=error, serial=captured_serial)
                        last_error = error.code
        finally:
            if self.native:
                try:
                    self.native.close()
                except ViewerError:
                    pass
            if self.process is not None:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if self.native is not None and self.native.owns():
                        self.process.terminate()
                        self.process.wait(timeout=3)
            if self.native:
                self.native.close_receiver()
            if self.cache is not None and (self.process is None or self.process.poll() is not None):
                try:
                    self.cache.close()
                except (OSError, ValueError):
                    self._emit("cleanup_deferred")
            try:
                self.playlist_after_close = self.profile.playlist_summary()
                self.profile.save_and_clean()
            except (OSError, ValueError):
                self._emit("cleanup_deferred")

    def close(self, *, wait=False):
        with self.lock:
            self.closed = True
            self.serial += 1
            self.intent = "stopped"
            self.messages.put(("close", self.serial, None))
        if wait:
            self.worker.join(10)
