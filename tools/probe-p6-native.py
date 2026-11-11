"""Anonymous dual-track native playback/seek probe using isolated data."""
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import sys
import tempfile
import time
ROOT = Path(__file__).resolve().parents[1];sys.path.insert(0, str(ROOT))
from live_viewer.vod import VodClient
from live_viewer.vod_proxy import VodProxy
from live_viewer.potplayer import PotPlayerAdapter
from live_viewer.events import safe_text
from potplayer_probe_support import original_settings_fingerprint

sys.stdout.reconfigure(encoding="utf-8")
result = {"success": False, "checks": {}}
before = original_settings_fingerprint()
def wait_for(fn, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():return True
        time.sleep(.1)
    return False
def check(name, value):
    result["checks"][name] = bool(value)
    print(name, bool(value), flush=True)
    if not value:raise RuntimeError(name)
def information(adapter):
    native = adapter.native
    native.user.PostMessageW(native.hwnd, 0x111, 10195, 0)
    time.sleep(.4)
    windows = []
    @native.enum
    def visit(window, _):
        pid = W.DWORD();native.user.GetWindowThreadProcessId(window, C.byref(pid))
        klass = C.create_unicode_buffer(100);native.user.GetClassNameW(window, klass, 100)
        if pid.value == adapter.process.pid and klass.value == "#32770":windows.append(window)
        return True
    native.user.EnumWindows(visit, 0)
    texts = []
    for dialog in windows:
        @native.enum
        def child(window, _):
            buffer = C.create_unicode_buffer(8192);returned = C.c_size_t()
            native.user.SendMessageTimeoutW(window, 0xD, 8192, C.addressof(buffer), 0x22, 500, C.byref(returned))
            texts.extend(safe_text(line) for line in buffer.value.splitlines()
                         if any(key in line.lower() for key in ("aac", "pcm", "audio codec", "audio renderer", "avc1", "hz", "khz")))
            return True
        native.user.EnumChildWindows.argtypes = [W.HWND, native.enum, W.LPARAM]
        native.user.EnumChildWindows(dialog, child, 0)
        native.user.PostMessageW(dialog, 0x10, 0, 0)
    return texts

with tempfile.TemporaryDirectory(prefix="p6-native-", dir=ROOT / ".research") as temporary:
    adapter = PotPlayerAdapter(Path(temporary), Path(r"D:\potplayer\PotPlayerMini64.exe"), hidden=True)
    proxy = None
    try:
        client = VodClient()
        try:source = client.resolve("BV17x411w7KC")
        finally:client.close()
        result["source"] = source.summary()
        proxy = VodProxy(source).prepare();proxy.start()
        adapter.prepare(replay=False)
        adapter.open(proxy.url, source.video.title, volume=0, mute=True, audio_url=proxy.audio_url)
        check("dual_track_picture_progress", wait_for(lambda: adapter.snapshot().get("owned_source") and
              adapter.snapshot().get("width", 0) > 0 and adapter.snapshot().get("position_seconds", 0) > 2))
        result["information"] = information(adapter)
        check("aac_native_audio_renderer", any("aac" in s.lower() for s in result["information"]) and
              any("audio renderer" in s.lower() for s in result["information"]))
        adapter.seek(90)
        check("native_seek_90_seconds", wait_for(lambda: 88 < adapter.snapshot().get("position_seconds", 0) < 96, 15))
        adapter.pause();check("native_pause", wait_for(lambda: adapter.snapshot().get("state") == "paused", 5))
        position = adapter.snapshot()["position_seconds"];time.sleep(2)
        adapter.resume();check("continue_keeps_position", wait_for(lambda: adapter.snapshot().get("state") == "playing", 5) and
              adapter.snapshot()["position_seconds"] < position + 5)
        result["native"] = adapter.snapshot()
        result["proxy"] = proxy.snapshot()
        check("no_media_confirmation", result["native"].get("visible_dialogs") == 0)
        adapter.stop();check("stop_delivered", wait_for(lambda: adapter.snapshot().get("state") == "closed_media", 5))
        result["success"] = True
    except Exception as exc:
        result["error"] = safe_text(str(exc))
    finally:
        adapter.close(wait=True)
        if proxy is not None:proxy.stop()
        result["checks"]["owned_process_exited"] = adapter.process is None or adapter.process.poll() is not None
        result["checks"]["original_settings_unchanged"] = original_settings_fingerprint() == before
        result["success"] = result["success"] and all(result["checks"].values())
path = ROOT / ".research/p6-native-validation.json"
path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False), flush=True)
raise SystemExit(0 if result["success"] else 1)
