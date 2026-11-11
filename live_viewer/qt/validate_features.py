"""P4 UI checks with synthetic login/diagnostic data in an isolated directory."""
import json
import threading
import time
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer
from ..core import Quality, cookies_from_text
from ..qr_login import Challenge, LoginState, qr_png
from .qr_dialog import QrLoginDialog


def validate_features(window, check, shot):
    def wait(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
    def pump(condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wait(30)
            window.tick()
            if condition():
                return True
        return False
    class WaitingFlow:
        def __init__(self, platform, **_):
            self.platform = platform
            self.cancelled = threading.Event()
        def create(self):
            return Challenge(qr_png("https://passport." + self.platform + ".com/synthetic-validation"), 180)
        def poll(self):
            self.cancelled.wait(2)
            return LoginState("cancelled")
        def cancel(self):
            self.cancelled.set()
        def close(self):
            pass
    def dialog_factory(*a, **kw):
        return QrLoginDialog(*a, **kw, flow_factory=WaitingFlow)
    def verified(platform, *_, **__):
        return {"verified": True if platform == "bilibili" else None,
                "message": "B 站测试登录有效" if platform == "bilibili" else "斗鱼测试登录载入，有效性未独立验证"}
    window.stop()
    window.go_page(1)
    jar = cookies_from_text("SESSDATA=P4-SYNTHETIC-OLD", "bilibili")
    with patch("live_viewer.qt.app.verify_login", side_effect=verified), \
         patch("live_viewer.qt.app.QrLoginDialog", side_effect=dialog_factory):
        window.remember_boxes["bilibili"].setChecked(True)
        window._import_login("bilibili", lambda: jar)
        check("p4_verified_import_and_encrypted_save", pump(lambda: "bilibili" in window.cookies and not window.runtime.jobs.pending))
        saved = window.credentials.directory / "bilibili.dpapi"
        check("p4_no_cleartext_credentials", saved.exists() and b"P4-SYNTHETIC-OLD" not in saved.read_bytes())
        window.qr_login("bilibili")
        dialog = window.qr_dialog
        check("p4_qr_png_visible", pump(lambda: dialog.image_label.pixmap() is not None and not dialog.image_label.pixmap().isNull()))
        shot("08-account-qr-synthetic")
        previous_generation = dialog.generation
        replacement = cookies_from_text("SESSDATA=P4-SYNTHETIC-NEW", "bilibili")
        dialog.queue.put((previous_generation, "success", replacement))
        dialog.refresh()
        dialog.poll()
        check("p4_refresh_rejects_old_qr_confirmation", window.cookies["bilibili"].get("SESSDATA") == "P4-SYNTHETIC-OLD")
        dialog.queue.put((dialog.generation, "success", replacement))
        dialog.poll()
        check("p4_new_qr_confirmation_replaces_saved_login", pump(lambda:
            window.qr_dialog is None and not window.runtime.jobs.pending and
            window.cookies["bilibili"].get("SESSDATA") == "P4-SYNTHETIC-NEW"))
        check("p4_disk_has_latest_login", window.credentials.load("bilibili").get("SESSDATA") == "P4-SYNTHETIC-NEW")
        window.qr_login("douyu")
        cancelled = window.qr_dialog
        cancelled.queue.put((cancelled.generation, "success", cookies_from_text("acf_auth=SYNTHETIC", "douyu")))
        window.clear_login("douyu")
        cancelled.poll()
        check("p4_clear_cancels_queued_qr_success", window.qr_dialog is None and "douyu" not in window.cookies)
        check("p4_clear_disk_finished", pump(lambda: not window.runtime.jobs.pending))
        window.remember_boxes["bilibili"].setChecked(False)
        check("p4_disable_remember_deletes_saved_login", pump(lambda: not window.runtime.jobs.pending and not saved.exists()))
        check("p4_disable_remember_keeps_live_session", "bilibili" in window.cookies)
        window.check_login("bilibili")
        check("p4_explicit_login_check", pump(lambda: not window.runtime.jobs.pending))
        shot("09-account-options")

    window.choose_room({"platform": "douyu", "room": "6657", "name": "画质检查对象"})
    window.mode_box.setCurrentIndex(0)
    sample = {"verification": "container_only", "decoded": False, "metadata": {"width": 1920, "height": 1080, "framerate": 60},
              "sample_total_kbps": 4120, "bytes_read": 204800}
    connection = {"platform": "douyu", "room_input": "6657", "network_mode": "direct", "stage": "完成",
                  "status": "接口与 FLV 读取成功，未验证解码。", "sample": sample}
    rows = [{"session": "匿名", "requested": 0, "actual": 0, "actual_name": "原画", "status": "流读取成功", "sample": sample},
            {"session": "已导入 Cookie", "status": "未导入 Cookie，账号对比未执行", "skipped": True}]
    with patch("live_viewer.qt.app.connection_check", return_value=connection), \
         patch("live_viewer.qt.app.compare_quality", return_value=(rows, [Quality(0, "原画"), Quality(1, "高清")])):
        window.go_page(4)
        window.diagnostic_target.setCurrentIndex(0)
        window.check_connection()
        window.compare()
        check("p4_independent_diagnostic_jobs_complete", pump(lambda: not window.runtime.jobs.pending))
        check("p4_diagnostic_target_and_quality_selection", window.comparison_request["room_input"] == "6657" and
              window.quality_box.findData(1) >= 0 and window.comparison_table.rowCount() == 2)
        check("p4_sample_does_not_claim_decoded_video", not window.connection_result["sample"]["decoded"])
        shot("10-diagnostics-results")
        export = window.store.directory / "p4-report-test.json"
        window.note.setText("Cookie: P4-SYNTHETIC-NEW")
        with patch("live_viewer.qt.app.QFileDialog.getSaveFileName", return_value=(str(export), "JSON")):
            window.export_report()
        check("p4_background_report_export", pump(lambda: window.report_button.isEnabled() and export.exists()))
        report = json.loads(export.read_text(encoding="utf-8"))
        check("p4_report_is_redacted_and_contains_comparison", "P4-SYNTHETIC-NEW" not in export.read_text(encoding="utf-8") and
              len(report["comparison"]) == 2)
    entered, release = threading.Event(), threading.Event()
    def delayed(*_, **__):
        entered.set()
        release.wait(2)
        return {**connection, "status": "过期结果"}
    with patch("live_viewer.qt.app.connection_check", side_effect=delayed):
        window.check_connection()
        check("p4_diagnostic_worker_started", entered.wait(1))
        window.cancel_diagnostics()
        release.set()
        wait(120)
        window.tick()
        check("p4_cancel_rejects_late_diagnostic_result", window.connection_result["status"] != "过期结果")
    window.clear_login("bilibili")
    check("p4_clear_removes_memory_and_disk_login", pump(lambda: not window.runtime.jobs.pending) and
          "bilibili" not in window.cookies and not saved.exists())
    window.go_page(2)
    window.replay_box.setChecked(True)
    window.replay_minutes_box.setCurrentIndex(window.replay_minutes_box.findData(300))
    window.vsr_box.setChecked(True)
    window.update_options()
    from ..settings import StateStore
    saved_settings = StateStore(window.store.directory).load()
    check("p5_replay_vsr_preferences_saved", saved_settings["native_replay"] and saved_settings["replay_minutes"] == 300
          and saved_settings["vsr"])
    check("p5_replay_disables_conflicting_realtime_guard", not window.low_latency.isEnabled())
    shot("11-replay-vsr-settings")
    window.vsr_box.setChecked(False)
