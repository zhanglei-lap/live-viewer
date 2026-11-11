"""Muted real-network Qt/Pot integration, with disposable data and owned processes."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from potplayer_probe_support import original_settings_fingerprint
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from live_viewer.qt.app import ViewerWindow
from live_viewer.potplayer import PotPlayerAdapter
from live_viewer.settings import StateStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--player", type=Path, default=Path(r"D:\potplayer\PotPlayerMini64.exe"))
    parser.add_argument("--output", type=Path, default=ROOT / ".research/p3-live-validation.json")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"success": False, "anonymous": True, "checks": {}}
    before = original_settings_fingerprint()
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet((ROOT / "live_viewer/qt/theme.qss").read_text(encoding="utf-8"))
    app.setQuitOnLastWindowClosed(False)
    def wait(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
    def pump(predicate, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wait(30)
            if predicate():
                return True
        return False
    def check(name, condition):
        report["checks"][name] = bool(condition)
        print(name + ": " + str(bool(condition)), flush=True)
        if not condition:
            raise RuntimeError(name)
    with tempfile.TemporaryDirectory(prefix="p3-live-", dir=args.output.parent) as temporary:
        store = StateStore(Path(temporary))
        config = store.load()
        config.update(player=str(args.player), volume=0, mute=True)
        store.save(config)
        window = ViewerWindow(store.directory)
        window.choose_room({"platform": "douyu", "room": "6657", "name": "斗鱼实播验证"})
        window.runtime.player = PotPlayerAdapter(store.directory, args.player, hidden=True)
        adapter = window.runtime.player
        window.show()
        beats = []
        timer = QTimer()
        timer.timeout.connect(lambda: beats.append(time.monotonic()))
        timer.start(50)
        def playing():
            snap = adapter.snapshot()
            return window.runtime.controller.state == "playing" and snap.get("owned_source") and (snap.get("position_seconds") or 0) > 1
        try:
            window.start()
            check("qt_start_reaches_owned_native_progress", pump(playing))
            address = window.runtime.relay.url
            report["initial_native"] = adapter.snapshot()
            window.grab().save(str(args.output.with_suffix(".watch.png")))
            first = window.runtime.serial
            window.reconnect()
            check("qt_manual_reconnect", pump(lambda: window.runtime.serial != first and playing()))
            check("stable_local_endpoint", window.runtime.relay.url == address)
            window.pause()
            check("qt_pause_observed", pump(lambda: window.runtime.paused and adapter.snapshot().get("state") == "paused", 8))
            serial = window.runtime.serial
            wait(2300)
            check("pause_does_not_reopen", window.runtime.serial == serial and window.runtime.controller.retry.paused)
            window.pause()
            check("qt_continue_reopens_latest_live", pump(lambda: window.runtime.serial != serial and playing()))
            serial = window.runtime.serial
            window.runtime.relay.disconnect_upstream()
            check("qt_automatic_recovery", pump(lambda: window.runtime.serial != serial and playing(), 70))
            check("recovery_clears_fixed_failure", window.activity.notice_for("playback") is None)
            check("pot_media_error_confirmation_absent", adapter.native.visible_dialog_count() == 0)
            check("history_persisted_on_received_media", StateStore(store.directory).load()["history"][0]["room"] == "6657")
            window.stop()
            check("qt_stop_closes_media", pump(lambda: adapter.snapshot().get("state") == "closed_media", 8))
            serial = adapter.serial
            wait(1600)
            check("stop_suppresses_auto_recovery", adapter.serial == serial and not window.runtime.controller.retry.active)
            window.start()
            check("qt_restart_after_stop", pump(playing))
            # Close the owned player while a worker is preparing a new stream.
            original_prepare = window.runtime.service.prepare
            def delayed(*a, **kw):
                time.sleep(.8)
                return original_prepare(*a, **kw)
            window.runtime.service.prepare = delayed
            window.reconnect()
            adapter.close_window()
            check("owned_window_close_cancels_pending_prepare", pump(lambda: window.runtime.active is None, 8))
            serial = adapter.serial
            wait(2500)
            check("closed_window_stays_closed", adapter.serial == serial and adapter.snapshot().get("state") == "closed")
            check("qt_timer_remained_responsive", len(beats) > 30 and max(b - a for a, b in zip(beats, beats[1:])) < 1)
            check("original_pot_settings_unchanged", original_settings_fingerprint() == before)
            report["events"] = [e.record() for e in window.activity.history]
            report["success"] = True
        except Exception as exc:
            report["error"] = type(exc).__name__ + ": " + str(exc)
            report["events"] = [e.record() for e in window.activity.history]
        finally:
            timer.stop()
            window.close()
            pump(lambda: window.closed, 15)
            report["shutdown_closed"] = window.closed
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["success"] and report["shutdown_closed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
