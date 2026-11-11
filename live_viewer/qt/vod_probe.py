"""Isolated anonymous validation of Qt + native Bilibili dual-track video playback."""
import ctypes as C
from ctypes import wintypes as W
import json
import sys
import time
from PySide6.QtCore import QEventLoop, QTimer
from ..events import safe_text
from ..potplayer import PotPlayerAdapter
from ..settings import StateStore, atomic_write


def audio_information(adapter):
    native = adapter.native
    native.user.PostMessageW(native.hwnd, 0x111, 10195, 0)
    loop = QEventLoop();QTimer.singleShot(450, loop.quit);loop.exec()
    dialogs, texts = [], []
    @native.enum
    def visit(window, _):
        pid = W.DWORD();native.user.GetWindowThreadProcessId(window, C.byref(pid))
        klass = C.create_unicode_buffer(100);native.user.GetClassNameW(window, klass, 100)
        if pid.value == adapter.process.pid and klass.value == "#32770":dialogs.append(window)
        return True
    native.user.EnumWindows(visit, 0)
    for dialog in dialogs:
        @native.enum
        def child(window, _):
            buffer = C.create_unicode_buffer(8192);returned = C.c_size_t()
            native.user.SendMessageTimeoutW(window, 0xD, 8192, C.addressof(buffer), 0x22, 500, C.byref(returned))
            texts.extend(safe_text(line) for line in buffer.value.splitlines()
                         if any(k in line.lower() for k in ("aac", "audio renderer", "audio codec", "hz")))
            return True
        native.user.EnumChildWindows.argtypes = [W.HWND, native.enum, W.LPARAM]
        native.user.EnumChildWindows(dialog, child, 0)
        native.user.PostMessageW(dialog, 0x10, 0, 0)
    return texts


def probe(app, window, args):
    result = {"success": False, "frozen": bool(getattr(sys, "frozen", False)), "anonymous": True,
              "checks": {}, "pause_seconds": args.probe_vod_pause_seconds}
    def wait(ms):
        loop = QEventLoop();QTimer.singleShot(ms, loop.quit);loop.exec()
    def pump(condition, timeout=45):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            wait(50)
            if condition():return True
        return False
    def check(name, value):
        result["checks"][name] = bool(value)
        if not value:raise RuntimeError(name)
    beats = [];timer = QTimer();timer.timeout.connect(lambda: beats.append(time.monotonic()));timer.start(100)
    page, runtime = window.vod_page, window.runtime
    runtime.player_factory = lambda *a: PotPlayerAdapter(*a, hidden=True)
    adapter = None
    try:
        window.go_page(5)
        window.config.update(volume=0, mute=True)
        window.player_path.setText(str(args.probe_player));window.update_options()
        page.input.setText(args.probe_video);page.lookup()
        check("metadata_and_both_tracks", pump(lambda: page.source is not None))
        check("ordinary_multi_part_video", len(page.source.video.pages) > 1)
        first = page.source
        page.name.setText("P6 匿名测试视频");page.save()
        page.name.setText("P6 收藏改名");page.save()
        restored = StateStore(window.store.directory).load()
        check("favorite_single_content_index", len(restored["video_favorites"]) == 1 and restored["video_favorites"][0]["name"] == "P6 收藏改名")
        page.play()
        def playing():
            return runtime.controller.state == "playing" and runtime.player.snapshot().get("owned_source") and runtime.player.snapshot().get("width", 0) > 0
        check("qt_owned_video_progress", pump(playing))
        adapter = runtime.player
        result["source"] = runtime.stream.summary()
        result["audio_information"] = audio_information(adapter)
        check("native_aac_and_audio_renderer", any("aac" in t.lower() for t in result["audio_information"]) and any("audio renderer" in t.lower() for t in result["audio_information"]))
        target = min(90, max(5, first.page["duration"] / 2))
        adapter.seek(target)
        check("native_seek_forward", pump(lambda: abs((adapter.snapshot().get("position_seconds") or 0) - target) < 4, 15))
        window.pause();check("qt_pause", pump(lambda: adapter.snapshot().get("state") == "paused", 8))
        wait(800)
        position = adapter.snapshot()["position_seconds"]
        result["pause_before"] = adapter.snapshot()
        pause_started = time.monotonic()
        while time.monotonic() - pause_started < args.probe_vod_pause_seconds:
            wait(100)
        result["pause_after"] = adapter.snapshot()
        check("long_pause_keeps_position", abs(adapter.snapshot()["position_seconds"] - position) < .1 and runtime.controller.state == "paused")
        window.pause();check("continue_same_position", pump(playing, 10) and adapter.snapshot()["position_seconds"] < position + 5)
        check("no_unwanted_live_retry", runtime.controller.retry.total_attempts == 0)
        result["native_first"] = adapter.snapshot()
        page.parts.setCurrentIndex(1)
        check("second_part_tracks", pump(lambda: page.source is not None and page.source.page["page"] == 2))
        page.play();check("second_part_owned_progress", pump(playing))
        check("old_video_player_released", adapter.process is None or adapter.process.poll() is not None)
        adapter = runtime.player
        result["source_second"] = runtime.stream.summary()
        check("second_part_cid_distinct", result["source_second"]["cid"] != result["source"]["cid"])
        adapter.seek(5);check("native_seek_backward", pump(lambda: 2 < adapter.snapshot().get("position_seconds", 0) < 9, 10))
        check("video_history_separate_from_rooms", len(window.config["video_history"]) == 2 and not window.config["history"])
        check("no_native_media_confirmation", adapter.snapshot().get("visible_dialogs") == 0)
        screenshot = args.probe_vod.with_suffix(".png");window.grab().save(str(screenshot))
        result["transport"] = runtime.relay.snapshot()
        duration = adapter.snapshot().get("duration_seconds") or 0
        adapter.seek(max(0, duration - 1))
        check("natural_completion_without_live_restart", pump(lambda: runtime.controller.state == "ended", 12))
        window.stop();check("stop_media", pump(lambda: adapter.snapshot().get("state") == "closed_media", 10))
        check("qt_responsive", len(beats) > 5 and max(b-a for a,b in zip(beats, beats[1:])) < 2)
        result["success"] = True
    except Exception as exc:
        result["error"] = safe_text(str(exc))
    finally:
        timer.stop()
        window.close();result["checks"]["shutdown"] = pump(lambda: window.closed, 20)
        result["checks"]["owned_process_exited"] = adapter is None or adapter.process is None or adapter.process.poll() is not None
        result["checks"]["temporary_video_cache_removed"] = not any((window.store.directory / "cache/live").glob("*/owner.json"))
        result["events"] = [e.record() for e in window.activity.history]
        result["success"] = result["success"] and all(result["checks"].values())
        atomic_write(args.probe_vod, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        app.exit(0 if result["success"] else 1)
