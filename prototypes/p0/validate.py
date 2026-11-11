"""P0 interaction checks and native Qt screenshots; all data stays in the demo."""
import json
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication


def wait(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def validate_prototype(application, window, output: Path, screenshots: Path | None):
    result = {"prototype": "P0", "qt_version": None, "success": False, "checks": {}, "screenshots": []}
    from PySide6 import __version__
    result["qt_version"] = __version__
    result["device_pixel_ratio"] = window.devicePixelRatioF()
    result["window_logical_size"] = [window.width(), window.height()]

    def check(name, condition):
        result["checks"][name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    def shot(name):
        if screenshots is not None:
            screenshots.mkdir(parents=True, exist_ok=True)
            wait(100)
            path = screenshots / (name + ".png")
            if not window.grab().save(str(path)):
                raise AssertionError("Screenshot failed")
            result["screenshots"].append(str(path.resolve()))

    try:
        check("five_pages", window.stack.count() == 5)
        check("initial_state_idle", window.model.state == "idle")
        shot("01-watch-ready")
        window.offline_demo()
        check("offline_is_warning_and_blocks_start",
              window.model.alert.level == "WARN" and window.model.alert.impact == "start_blocked"
              and window.model.state == "unavailable")
        check("offline_has_visible_banner", window.notice.isVisible())
        shot("02-room-offline")

        window.tls_demo()
        check("tls_strong_notification_once", window.notification_count == 1 and window.strong_dialog is not None)
        window.close_strong_notice()
        title = window.status_title.text()
        code = window.model.alert.code
        window.login_success_demo()
        check("login_success_does_not_override_failure",
              window.model.state == "error" and window.status_title.text() == title
              and window.model.alert.code == code and window.model.events[-1].level == "INFO")
        shot("03-tls-error-with-login-success")
        count = window.notification_count
        window.repeat_tls_demo()
        check("repeated_failure_logs_without_extra_popup",
              window.notification_count == count and window.strong_dialog is None
              and len([e for e in window.model.events if e.code == "network.tls_failed"]) == 2)
        window.toggle_notice_details()
        shot("04-expanded-error-detail")
        for index in range(5):
            window.go_page(index)
            check(f"persistent_notice_page_{index}",
                  window.notice.isVisible() and window.notice.geometry().bottom() < window.stack.geometry().top())
        window.go_page(0)
        window.stop_demo()
        check("manual_stop_is_info_and_clears_current_alert",
              window.model.state == "stopped" and window.model.events[-1].level == "INFO"
              and window.model.alert is None and not window.notice.isVisible())
        shot("05-manual-stop")

        window.recovery_demo(True)
        window.stop_demo()
        wait(2350)
        check("user_stop_rejects_late_success",
              window.model.state == "stopped" and not any(e.code == "playback.recovered" for e in window.model.events))
        check("late_results_are_only_debug", any(e.code == "task.stale_ignored" and e.level == "DEBUG"
                                                for e in window.model.events))
        window.recovery_demo(True)
        before = window.notification_count
        shot("06-reconnecting")
        wait(2350)
        check("automatic_recovery_clears_current_failure",
              window.model.state == "playing" and window.model.alert is None and window.notification_count == before)
        check("retry_attempts_are_logged", len([e for e in window.model.events if e.code == "playback.retry_attempt"]) == 3)
        window.recovery_demo(False)
        wait(2350)
        check("retry_exhaustion_is_one_strong_error",
              window.model.state == "error" and window.notification_count == before + 1
              and window.model.alert.code == "playback.recovery_exhausted")
        window.close_strong_notice()
        window.recovery_options["strong"].setChecked(False)
        before = window.notification_count
        window.tls_demo()
        check("strong_preference_keeps_banner_without_popup",
              window.notice.isVisible() and window.strong_dialog is None and window.notification_count == before)
        window.recovery_options["strong"].setChecked(True)
        window.recovery_demo(True)
        window.recovery_options["auto"].setChecked(False)
        wait(2350)
        check("disabling_auto_recovery_cancels_pending_result",
              window.model.state == "error" and window.model.alert.code == "playback.auto_disabled")
        window.recovery_options["auto"].setChecked(True)
        window.recovery_demo(False)
        window.stop_demo()
        before = window.notification_count
        wait(2350)
        check("manual_stop_rejects_late_error_popup",
              window.model.state == "stopped" and window.model.alert is None and window.notification_count == before)
        window.tls_demo()
        window.close_strong_notice()
        window.go_page(3)
        window.level_filter.setCurrentText("ERROR")
        window.log_search.setText("证书")
        check("log_filter_matches_level_and_text",
              window.log_table.rowCount() >= 2 and all(e.level == "ERROR" for e in window.filtered_events))
        window.log_table.selectRow(0)
        wait(50)
        check("log_detail_contains_event_code", "network.tls_failed" in window.log_detail.toPlainText())
        shot("07-log-filter-and-detail")
        window.level_filter.setCurrentText("全部等级")
        window.log_search.clear()
        window.go_page(1);shot("08-account")
        window.show_login_demo("斗鱼");wait(50)
        check("qr_is_layout_only", window.account_dialog is not None)
        window.account_dialog.reject()
        window.go_page(2);shot("09-settings")
        window.go_page(4);shot("10-diagnostics")
        window.stop_demo()
        window.go_page(0)
        original_size = window.size()
        window.resize(920, 620);wait(100)
        check("minimum_width_is_usable", window.status_card.width() >= 380)
        check("notice_is_outside_scroll_pages", window.notice.parent() is window.stack.parent())
        shot("11-minimum-window")
        window.resize(original_size)
        serialized = json.dumps(window.model.export(), ensure_ascii=False)
        check("export_is_explicitly_demo", '"real_playback": false' in serialized and '"demo": true' in serialized)
        check("export_has_no_signed_urls_or_credentials",
              all(value not in serialized for value in ("https://", "Cookie=", "SESSDATA=", "qrcode_key=", "auth=")))
        check("all_events_follow_contract",
              all(e.level in ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
                  and e.schema_version == 1 and e.demo for e in window.model.events))
        result["event_count"] = len(window.model.events)
        result["success"] = all(result["checks"].values())
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_check"] = str(exc)
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        window.close()
        application.exit(0 if result["success"] else 1)
