"""Isolated, muted P2 integration probe. Never use the user's data or player."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from potplayer_probe_support import original_settings_fingerprint

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from live_viewer.app import ViewerApp
from live_viewer.potplayer import PotPlayerAdapter
from live_viewer.settings import StateStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--player", type=Path, default=Path(r"D:\potplayer\PotPlayerMini64.exe"))
    parser.add_argument("--output", type=Path, default=ROOT / ".research/p2-live-probe.json")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"success": False, "checks": {}, "steps": []}
    original_settings = original_settings_fingerprint()
    with tempfile.TemporaryDirectory(prefix="p2-live-", dir=args.output.parent) as temporary:
        directory = Path(temporary)
        store = StateStore(directory)
        config = store.load()
        config.update(playback_backend="potplayer", player=str(args.player), volume=0, mute=True)
        store.save(config)
        root = tk.Tk();root.withdraw()
        app = ViewerApp(root, directory)
        app.potplayer = PotPlayerAdapter(directory, args.player, hidden=True)
        adapter = app.potplayer
        app.platform.set("斗鱼");app.room.set("6657")
        def pump(condition, timeout=45):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                root.update()
                if condition():
                    return True
                time.sleep(.025)
            return False
        def playing():
            snap = adapter.snapshot()
            return (not app.busy and snap.get("state") == "playing" and snap.get("owned_source")
                    and snap.get("width", 0) > 0 and (snap.get("position_seconds") or 0) > 1)
        try:
            app.play()
            if not pump(playing):
                raise RuntimeError("Initial owned playback not ready")
            first = adapter.snapshot()
            address = app.relay.url
            report["initial"] = first
            report["checks"]["owned_native_playback"] = True
            for index in range(3):
                serial = app.pot_serial
                app.reconnect()
                if not pump(lambda: app.pot_serial != serial and playing()):
                    raise RuntimeError("Manual reconnect not ready")
                report["steps"].append({"reconnect": index + 1, "snapshot": adapter.snapshot(),
                                         "same_endpoint": app.relay.url == address})
            report["checks"]["stable_address_three_reconnects"] = all(step["same_endpoint"] for step in report["steps"])
            original = app.pot_serial
            app.relay.disconnect_upstream()
            if not pump(lambda: app.pot_serial != original and playing(), timeout=60):
                raise RuntimeError("Automatic recovery not ready")
            report["automatic_recovery"] = adapter.snapshot()
            report["checks"]["automatic_recovery"] = True
            # This version's live list is a temporary album, intentionally not
            # persisted to DPL. Capture its actual rendered list after recovery.
            import ctypes
            from ctypes import wintypes as W
            from PIL import ImageGrab
            adapter.hidden = False
            adapter.native.user.ShowWindow(adapter.native.hwnd, 4)
            rect = W.RECT()
            adapter.native.user.GetClientRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
            adapter.native.user.GetClientRect(adapter.native.hwnd, ctypes.byref(rect))
            x, y = rect.right - 26, rect.bottom - 25
            adapter.native.send(1, (y << 16) | x, message=0x201)
            adapter.native.send(0, (y << 16) | x, message=0x202)
            pump(lambda: adapter.snapshot().get("state") == "playing", timeout=1)
            root.update();time.sleep(.3)
            screenshot = args.output.with_suffix(".playlist.png")
            ImageGrab.grab(window=adapter.native.hwnd).save(screenshot)
            report["playlist_visual_check"] = str(screenshot)
            adapter.hidden = True
            adapter.pause()
            if not pump(lambda: app.paused_intent and adapter.snapshot().get("state") == "paused", timeout=5):
                raise RuntimeError("Pause not observed")
            paused_serial = app.pot_serial
            started = time.monotonic()
            pump(lambda: time.monotonic() - started > 5, timeout=7)
            report["checks"]["pause_suppresses_recovery"] = app.pot_serial == paused_serial and app.retry.paused
            adapter.resume()
            if not pump(lambda: app.pot_serial != paused_serial and playing()):
                raise RuntimeError("Resume not ready")
            app.stop_playback()
            if not pump(lambda: adapter.snapshot().get("state") == "closed_media", timeout=5):
                raise RuntimeError("Stop not delivered")
            stopped_serial = adapter.serial
            started = time.monotonic()
            pump(lambda: time.monotonic() - started > 2, timeout=4)
            report["checks"]["stop_suppresses_recovery"] = not app.retry.active and adapter.serial == stopped_serial
            app.play()
            if not pump(playing):
                raise RuntimeError("Playback after stop not ready")
            external_serial = adapter.serial
            adapter.close_media()
            if not pump(lambda: app.active_spec is None and adapter.snapshot().get("state") == "closed_media", timeout=6):
                raise RuntimeError("External close playback not observed")
            started = time.monotonic()
            pump(lambda: time.monotonic() - started > 2, timeout=4)
            report["checks"]["external_stop_suppresses_recovery"] = not app.retry.active and adapter.serial == external_serial
            app.play()
            if not pump(playing):
                raise RuntimeError("Playback before close not ready")
            # Delay only this test's resolver. Close the actual owned player
            # while a new upstream attempt is pending, then release the result.
            import threading
            from unittest.mock import patch
            entered, release = threading.Event(), threading.Event()
            real_prepare = app.playback_service.prepare
            def delayed(*a, **kw):
                entered.set()
                release.wait(5)
                return real_prepare(*a, **kw)
            with patch.object(app.playback_service, "prepare", side_effect=delayed):
                app.reconnect()
                if not entered.wait(2):
                    raise RuntimeError("Delayed worker did not start")
                adapter.close_window()
                if not pump(lambda: app.active_spec is None and not app.busy, timeout=6):
                    raise RuntimeError("Window close did not cancel pending work")
                release.set()
                started = time.monotonic()
                pump(lambda: time.monotonic() - started > 3, timeout=5)
                report["checks"]["close_cancels_pending_reconnect"] = not app.retry.active and app.active_spec is None and adapter.snapshot().get("state") == "closed"
            report["success"] = all(report["checks"].values())
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            report["last_snapshot"] = adapter.snapshot()
            report["state"] = app.controller.snapshot()
        finally:
            report["events"] = app.activity.store.read(limit=200)
            app.close()
            adapter.close(wait=True)
            report["owned_process_exited"] = adapter.process is None or adapter.process.poll() is not None
            report["playlist_after_close"] = adapter.playlist_after_close
            albums = (adapter.playlist_after_close or {}).get("albums", [])
            report["playlist_persistence_enabled"] = bool(albums)
            report["checks"]["no_persisted_stale_items"] = all(row["items"] <= 1 and row["distinct_sources"] <= 1 and row["only_loopback"] for row in albums)
            report["success"] &= report["checks"]["no_persisted_stale_items"]
            report["checks"]["original_settings_unchanged"] = original_settings == original_settings_fingerprint()
            report["success"] &= report["owned_process_exited"]
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": report["success"], "checks": report["checks"], "error_type": report.get("error_type"),
                      "last_snapshot": report.get("last_snapshot")}, ensure_ascii=True), flush=True)
    raise SystemExit(0 if report["success"] else 1)


if __name__ == "__main__":
    main()
