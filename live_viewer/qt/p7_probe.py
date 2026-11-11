"""Anonymous P7 integration checks and optional sustained native video soak."""
from dataclasses import replace
import json
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import requests
from PySide6.QtCore import QEventLoop, QTimer
from ..core import new_session
from ..potplayer import PotPlayerAdapter
from ..settings import StateStore, atomic_write
from ..subtitles import Subtitle, to_srt
from .vod_probe import audio_information


def probe(app, window, args):
    report = {"success": False, "frozen": bool(getattr(sys, "frozen", False)), "anonymous": True,
              "checks": {}, "samples": [], "minutes_requested": args.probe_p7_minutes, "codecs": []}
    def wait(ms):
        loop = QEventLoop();QTimer.singleShot(ms, loop.quit);loop.exec()
    def pump(fn, seconds=50):
        limit = time.monotonic() + seconds
        while time.monotonic() < limit:
            wait(50)
            if fn():return True
        return False
    def check(name, value):
        report["checks"][name] = bool(value)
        print(name, bool(value), flush=True)
        if not value:raise RuntimeError(name)
    page, runtime = window.vod_page, window.runtime
    runtime.player_factory = lambda *a: PotPlayerAdapter(*a, hidden=True)
    original_prepare = runtime.service.prepare
    def with_caption(*a, **kw):
        relay, source = original_prepare(*a, **kw)
        if a[0].get("kind") == "vod":
            source.subtitle = Subtitle("1:test", "test", "合成验证字幕", "https://i0.hdslb.com/bfs/subtitle/test.json")
            source.subtitle_text = to_srt({"body": [{"from": 0, "to": source.page["duration"], "content": "P7 字幕加载验证 / subtitle check"}]})
        return relay, source
    runtime.service.prepare = with_caption
    timer = QTimer();beats = [];timer.timeout.connect(lambda: beats.append(time.monotonic()));timer.start(100)
    adapter = None
    def playing():return runtime.controller.state == "playing" and runtime.player.snapshot().get("owned_source") and runtime.player.snapshot().get("width", 0) > 0
    def save_partial():atomic_write(args.probe_p7, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    try:
        window.go_page(5);window.config.update(volume=0, mute=True)
        window.player_path.setText(str(args.probe_player));window.update_options()
        page.input.setText("BV17Ppi6dE6Y");page.lookup()
        check("user_video_metadata", pump(lambda: page.source is not None))
        page.load_subtitles();check("user_subtitle_catalog_finished", pump(lambda: "vod.subtitles" not in runtime.jobs.pending))
        report["user_subtitle_status"] = page.subtitle_status.text()
        page.input.setText("BV17x411w7KC");page.lookup()
        check("codec_catalog_ready", pump(lambda: page.source is not None))
        tracks = page.source.videos
        page.resume_box.setChecked(False)
        for prefix in ("avc1", "hev", "av01"):
            track = next((t for t in tracks if t.codec.startswith(prefix) or prefix == "hev" and t.codec.startswith("hvc")), None)
            check("platform_returns_" + prefix, track is not None)
            page.videos.setCurrentIndex(page.videos.findData(track.key));page.play()
            check("native_decode_" + prefix, pump(playing))
            adapter = runtime.player
            report["codecs"].append({"codec": track.codec, "native": adapter.snapshot()})
            check("caption_file_" + prefix, (adapter.cache.path / "captions.srt").is_file())
            save_partial()
        adapter.seek(30);check("precise_seek", pump(lambda: abs(adapter.snapshot().get("position_seconds", 0) - 30) < 1.5, 12))
        wait(1000);window.pause();check("pause_before_refresh", pump(lambda: adapter.snapshot().get("state") == "paused", 6));wait(800)
        position = adapter.snapshot()["position_seconds"]
        relay = runtime.relay;serial = runtime.serial
        def expire(url):
            parsed = urlparse(url);params = dict(parse_qsl(parsed.query));params["deadline"] = "1"
            if "upsig" in params:params["upsig"] = "0"
            if "wsTime" in params:params["wsTime"] = "00000001"
            if "wsSecret" in params:params["wsSecret"] = "0"
            return urlunparse(parsed._replace(query=urlencode(params)))
        relay.tracks["vod-video.mp4"] = replace(relay.tracks["vod-video.mp4"], urls=tuple(expire(u) for u in relay.tracks["vod-video.mp4"].urls))
        ranged = {}
        def get_range():
            with new_session() as session:
                start = relay.sizes["vod-video.mp4"] // 2
                with session.get(relay.url, headers={"Range": f"bytes={start}-{start+1023}"}, timeout=(5, 45)) as response:
                    return response.status_code, len(response.content)
        runtime.jobs.submit("probe.range", get_range, lambda result: ranged.update(result=result), lambda exc: ranged.update(error=exc.code))
        check("expired_url_range_recovers", pump(lambda: bool(ranged), 75) and ranged.get("result") == (206, 1024) and relay.refreshes > 0)
        check("warm_refresh_keeps_item_pause_position", runtime.serial == serial and adapter.snapshot()["state"] == "paused" and abs(adapter.snapshot()["position_seconds"] - position) < .1)
        report["warm_refreshes"] = relay.refreshes
        runtime.reconnect()
        check("cold_recovery_keeps_pause_time", pump(lambda: runtime.player is not adapter and runtime.player.snapshot().get("state") == "paused" and runtime.restore_target is None, 60))
        adapter = runtime.player
        check("cold_recovery_position", abs(adapter.snapshot().get("position_seconds", 0) - position) < 2)
        window.pause();check("continue_after_recovery", pump(playing, 10))
        window.stop();saved = StateStore(window.store.directory).load()["video_progress"]
        check("bookmark_persisted", saved["BV17x411w7KC:279786"]["position"] > 20)
        page.resume_box.setChecked(True);page.play();check("bookmark_relaunch", pump(playing))
        check("bookmark_position_restored", pump(lambda: runtime.restore_target is None and runtime.player.snapshot().get("position_seconds", 0) > 20, 20))
        adapter = runtime.player;report["audio_information"] = audio_information(adapter)
        # Produce a screenshot of this owned player for subtitle visual QA.
        adapter.hidden = False;native = adapter.native
        native.user.ShowWindow(native.hwnd, 4);native.user.SetForegroundWindow(native.hwnd);wait(700)
        from PIL import ImageGrab
        import ctypes as C
        from ctypes import wintypes as W
        rect = W.RECT();native.user.GetWindowRect(native.hwnd, C.byref(rect))
        if native.owns() and native.user.GetForegroundWindow() == native.hwnd:
            path = args.probe_p7.with_name(args.probe_p7.stem + "-native.png")
            ImageGrab.grab((rect.left,rect.top,rect.right,rect.bottom)).save(path);report["subtitle_screenshot"] = str(path)
        adapter.hidden = True
        started = last_sample = time.monotonic()
        while time.monotonic() - started < args.probe_p7_minutes * 60:
            wait(100)
            if runtime.controller.state == "ended":
                page.play();check("soak_explicit_next_cycle", pump(playing));adapter = runtime.player
            if runtime.controller.state == "error":raise RuntimeError("soak_unrecovered_error")
            if time.monotonic() - last_sample > 30:
                report["samples"].append({"elapsed": round(time.monotonic()-started,1), "state": runtime.controller.state,
                    "native": runtime.player.snapshot(), "transport": runtime.relay.snapshot() if runtime.relay else {}})
                save_partial();last_sample = time.monotonic()
        report["elapsed_seconds"] = round(time.monotonic()-started,1)
        check("qt_responsive", len(beats)>5 and max(b-a for a,b in zip(beats,beats[1:]))<2)
        check("no_native_dialogs", runtime.player.snapshot().get("visible_dialogs") == 0)
        window.stop();check("stop_media", pump(lambda: runtime.player.snapshot().get("state") == "closed_media", 10))
        report["success"] = True
    except Exception as exc:
        report["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        timer.stop();window.close();report["checks"]["shutdown"] = pump(lambda: window.closed, 20)
        report["checks"]["owned_process_exited"] = adapter is None or adapter.process is None or adapter.process.poll() is not None
        report["checks"]["temporary_caption_and_cache_removed"] = not any((window.store.directory / "cache/live").glob("*/owner.json"))
        report["events"] = [e.record() for e in window.activity.history]
        report["success"] = report["success"] and all(report["checks"].values());save_partial()
        app.exit(0 if report["success"] else 1)
