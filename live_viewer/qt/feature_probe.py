"""Real QR issuance/polling and anonymous diagnostics, never phone confirmation."""
import json
import sys
import time
from PySide6.QtCore import QEventLoop, QTimer
from ..settings import atomic_write


def probe(app, window, args):
    result = {"success": False, "frozen": bool(getattr(sys, "frozen", False)),
              "anonymous": True, "mobile_confirmation_tested": False, "checks": {}, "qr": {}}
    window.hide()
    def wait(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
    def pump(condition, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wait(50)
            if condition():
                return True
        return False
    def check(name, value):
        result["checks"][name] = bool(value)
        if not value:
            raise RuntimeError(name)
    try:
        for platform in ("bilibili", "douyu"):
            window.qr_login(platform)
            dialog = window.qr_dialog
            dialog.hide()  # Do not expose live login challenges during an anonymous probe.
            pump(lambda: dialog.last_state in ("waiting", "scanned", "error", "expired"), 45)
            pixmap = dialog.image_label.pixmap()
            generated = pixmap is not None and not pixmap.isNull()
            result["qr"][platform] = {"png_generated": generated, "state": dialog.last_state,
                                       "windows_tls_fallback": dialog.flow.http.windows_tls if dialog.flow else None}
            check(platform + "_qr_issued_and_polled", generated and dialog.last_state == "waiting")
            window.cancel_qr(platform)
            check(platform + "_qr_cancelled", dialog.closed)
        window.choose_room({"platform": "douyu", "room": "6657", "name": "匿名诊断验证"}, log=False)
        window.mode_box.setCurrentIndex(0)
        window.check_connection()
        check("douyu_connection_check_completed", pump(lambda: "diagnostic.connection" not in window.runtime.jobs.pending, 90))
        check("douyu_container_read", window.connection_result is not None and not window.connection_result.get("error_code"))
        result["connection"] = window.connection_result
        window.compare()
        check("douyu_quality_compare_completed", pump(lambda: "diagnostic.compare" not in window.runtime.jobs.pending, 90))
        check("douyu_anonymous_sample_and_account_skipped", bool(window.comparison_rows) and
              not window.comparison_rows[0].get("error_code") and window.comparison_rows[1].get("skipped") is True)
        result["comparison"] = window.comparison_rows
        window.choose_room({"platform": "bilibili", "room": "35", "name": "B 站接口验证"}, log=False)
        window.mode_box.setCurrentIndex(0)
        window.check_connection()
        check("bilibili_connection_check_completed", pump(lambda: "diagnostic.connection" not in window.runtime.jobs.pending, 60))
        result["bilibili_connection"] = window.connection_result
        check("bilibili_read_or_typed_offline", window.connection_result is not None and
              (not window.connection_result.get("error_code") or window.connection_result.get("error_code") == "room.offline"))
        check("no_account_was_imported", not window.cookies)
        check("no_credential_files_created", not any(window.credentials.directory.glob("*.dpapi")))
        result["events"] = [e.record() for e in window.activity.history]
        result["success"] = True
    except Exception as exc:
        result["error"] = str(exc) if isinstance(exc, RuntimeError) else "匿名功能探针未完成，请查看脱敏事件。"
        result["events"] = [e.record() for e in window.activity.history]
    finally:
        window.close()
        result["checks"]["responsive_shutdown"] = pump(lambda: window.closed, 20)
        result["success"] = result["success"] and result["checks"]["responsive_shutdown"]
        atomic_write(args.probe_features, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        app.exit(0 if result["success"] else 1)
