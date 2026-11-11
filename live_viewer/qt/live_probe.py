"""Optional anonymous integration probe of the actual packaged Qt entry point."""
import json
import sys
import time
from PySide6.QtCore import QEventLoop, QTimer
from ..potplayer import PotPlayerAdapter
from ..settings import atomic_write


def probe(app, window, args):
    report = {"success": False, "frozen": bool(getattr(sys, "frozen", False)), "anonymous": True,
              "platform": args.probe_platform, "room": args.probe_room, "checks": {}}
    def wait(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
    def pump(condition, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wait(40)
            if condition():
                return True
        return False
    try:
        window.choose_room({"platform": args.probe_platform, "room": args.probe_room, "name": "匿名实播验证"}, log=False)
        window.mode_box.setCurrentIndex(0)
        window.config.update(volume=0, mute=True)
        window.player_path.setText(str(args.probe_player))
        window.update_options()
        window.runtime.player = PotPlayerAdapter(window.store.directory, args.probe_player, hidden=True)
        adapter = window.runtime.player
        window.start()
        ready = pump(lambda: window.runtime.controller.state in ("playing", "unavailable", "error"), 50)
        report["checks"]["request_completed"] = ready
        if window.runtime.controller.state == "playing":
            adapter = window.runtime.player
            native = adapter.snapshot()
            report["native"] = native
            report["stream"] = window.runtime.stream.summary()
            report["checks"]["owned_progress_and_dimensions"] = bool(
                native.get("owned_source") and native.get("width", 0) > 0 and
                native.get("height", 0) > 0 and (native.get("position_seconds") or 0) > 0)
            report["checks"]["no_media_confirmation_dialog"] = native.get("visible_dialogs") == 0
            report["checks"]["history_recorded"] = bool(window.config["history"])
            window.grab().save(str(args.probe_live.with_suffix(".png")))
            window.stop()
            report["checks"]["stop_closes_owned_media"] = pump(
                lambda: adapter.snapshot().get("state") == "closed_media", 8)
            report["played"] = True
        elif window.runtime.controller.state == "unavailable":
            report["played"] = False
            report["checks"]["offline_is_typed_warning"] = window.notice_event is not None and window.notice_event.code == "room.offline" \
                and window.notice_event.level == "WARN" and window.notice.isVisible()
        else:
            report["checks"]["playback_available"] = False
        report["events"] = [e.record() for e in window.activity.history]
        report["success"] = all(report["checks"].values())
    except Exception:
        report["error"] = "实播探针未完成，检查脱敏事件。"
        report["events"] = [e.record() for e in window.activity.history]
    finally:
        window.close()
        report["checks"]["responsive_shutdown"] = pump(lambda: window.closed, 15)
        report["success"] = report["success"] and report["checks"]["responsive_shutdown"]
        atomic_write(args.probe_live, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
        app.exit(0 if report["success"] else 1)
