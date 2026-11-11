"""Offline interaction/packaging checks; synthetic accounts, no real login or playback."""
import json
from pathlib import Path
import threading
import time
import sys

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QApplication

from ..events import Event
from ..settings import StateStore, atomic_write


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def validate(app, window, output, screenshots=None):
    result = {"stage": "P8-B", "success": False, "offline": True, "checks": {}, "screenshots": [],
              "frozen": bool(getattr(sys, "frozen", False)),
              "device_pixel_ratio": window.devicePixelRatioF(),
              "logical_window_size": [window.width(), window.height()]}
    def check(name, value):
        result["checks"][name] = bool(value)
        if not value:
            raise AssertionError(name)
    def shot(name):
        if screenshots:
            screenshots.mkdir(parents=True, exist_ok=True)
            wait(80)
            file = screenshots / (name + ".png")
            check("screenshot_" + name, window.grab().save(str(file)))
            result["screenshots"].append(str(file.resolve()))
            if window.qr_dialog is not None:
                qr_file = screenshots / (name + "-dialog.png")
                check("screenshot_" + name + "_dialog", window.qr_dialog.grab().save(str(qr_file)))
                result["screenshots"].append(str(qr_file.resolve()))
    try:
        from unittest.mock import patch
        foreground_patch = patch("live_viewer.qt.notifications.foreground", return_value=True)
        foreground_patch.start()
        check("seven_functional_pages", window.stack.count() == 7)
        check("no_prototype_data_or_events", all(not r["demo"] for r in window.log_model.rows))
        shot("01-watch-ready")
        window.choose_room({"platform": "douyu", "room": "6657", "name": "长名称测试"})
        window.room_name.setText("我的玩机器")
        window.save_room()
        window.room_name.setText("新的显示名称")
        window.save_room()
        window.tick()
        restored = StateStore(window.store.directory).load()
        matches = [r for r in restored["favorites"] if r["platform"] == "douyu" and r["room"] == "6657"]
        check("rename_retains_one_numeric_index", len(matches) == 1 and matches[0]["name"] == "新的显示名称")
        window.room_search.setText("新的显示")
        check("favorites_search_name", window.room_list.count() == 1)
        window.room_search.setText("6657")
        check("favorites_search_number", window.room_list.count() == 1)
        window.room_search.clear()
        window.config["history"] = [{"platform": "douyu", "room": "6657", "name": "新的显示名称", "played_at": "2026-10-11"}]
        window.save_settings()
        window.room_tab.setCurrentIndex(1)
        check("history_real_list", window.room_list.count() == 1)
        window.room_list.setCurrentRow(0)
        check("history_selects_room", window.room_input.text() == "6657")
        window.room_tab.setCurrentIndex(0)

        request = {"platform": "bilibili", "room_input": "35", "network_mode": "direct"}
        token = window.runtime.controller.begin(request)
        window.runtime.controller.fail(token, __import__("live_viewer.core", fromlist=["ViewerError"]).ViewerError(
            "该 B 站房间当前未开播（轮播不视为直播）。", code="room.offline", level="WARN"))
        window.tick()
        check("offline_fixed_warning", window.notice.isVisible() and window.notice_event.code == "room.offline")
        shot("02-room-offline")
        error = __import__("live_viewer.core", fromlist=["ViewerError"]).ViewerError(
            "证书连接失败（stream-beijing-edu-121-194-9-3.edgesrv.com；直连）。", code="network.tls_failed")
        window.runtime.controller.fail(token, error)
        window.tick()
        check("tls_one_nonblocking_strong_notice", window.notification_count == 1 and window.strong_dialog is not None)
        existing_dialog = window.strong_dialog
        window.runtime.controller.fail(token, error)
        window.tick()
        check("repeat_error_keeps_existing_unconfirmed_reminder", window.strong_dialog is existing_dialog)
        window.close_strong_notice()
        window.activity.emit("account.imported", "登录已载入。", source="account")
        window.tick()
        check("login_success_cannot_replace_playback_fault", window.notice_event.code == "network.tls_failed")
        shot("03-tls-after-account-success")
        count = window.notification_count
        window.runtime.controller.fail(token, error)
        window.tick()
        check("repeat_error_logged_without_popup", window.notification_count == count and
              len([r for r in window.log_model.rows if r["code"] == "network.tls_failed"]) == 3)
        for i in range(5):
            window.go_page(i)
            wait(40)
            check("fixed_notice_page_" + str(i), window.notice.isVisible() and
                  window.notice.geometry().bottom() < window.stack.geometry().top())
        window.go_page(3)
        window.activity.emit("diagnostic.sanitized", "Cookie: secret-test-cookie", source="diagnostic", level="ERROR",
                             target="douyu:6657", details={"host": "example.com", "cookie": "secret-test-cookie",
                                                         "url": "https://example.com/token?secret"})
        window.tick()
        window.level_filter.setCurrentIndex(window.level_filter.findData("ERROR"))
        window.source_filter.setCurrentIndex(window.source_filter.findData("diagnostic"))
        window.target_filter.setText("6657")
        window.log_search.setText("diagnostic.sanitized")
        check("combined_level_source_target_search", window.log_filter.rowCount() == 1)
        window.log_table.selectRow(0)
        check("selected_event_details", "diagnostic.sanitized" in window.log_detail.toPlainText())
        # Preserve the clipboard: validation does not replace the user's clipboard.
        check("details_redacted", "secret-test-cookie" not in window.log_detail.toPlainText() and
              "https://example" not in window.log_detail.toPlainText())
        export = window.store.directory / "filtered-test.json"
        count = window.log_filter.export(export)
        payload = json.loads(export.read_text(encoding="utf-8"))
        check("filtered_export_only_matching_redacted_events", count == 1 and len(payload["events"]) == 1 and
              "secret-test-cookie" not in export.read_text(encoding="utf-8"))
        shot("04-log-filter-details")
        window.reset_filters()
        check("reset_filters_restores_all_events", window.log_filter.rowCount() == window.log_model.rowCount())
        window.go_page(2)
        window.player_path.setText(r"D:\potplayer\PotPlayerMini64.exe")
        window.update_options()
        window.low_latency.setChecked(False)
        window.strong_notifications.setChecked(False)
        restored = StateStore(window.store.directory).load()
        check("settings_saved_and_restored", restored["player"] == r"D:\potplayer\PotPlayerMini64.exe" and
              not restored["low_latency"] and not restored["ui"]["strong_notifications"])
        shot("05-settings")
        window.runtime.controller.fail(token, error)
        window.tick()
        check("strong_preference_keeps_fixed_banner", window.strong_dialog is None and window.notice.isVisible())
        window.stop()
        window.go_page(0)
        check("manual_stop_info_and_clear_banner", window.runtime.controller.state == "stopped" and not window.notice.isVisible())
        shot("06-stopped")
        beats = []
        heartbeat = QTimer()
        heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))
        heartbeat.start(20)
        completed = []
        window.runtime.jobs.submit("diagnostic.slow_test", lambda: time.sleep(.6), lambda _: completed.append(True),
                                   lambda _: completed.append(False))
        wait(800)
        heartbeat.stop()
        check("gui_responsive_during_background_task", len(beats) >= 15 and completed == [True])
        # Disk history survives restart without replaying old errors as notices.
        from .app import ViewerWindow
        other = ViewerWindow(window.store.directory)
        other.show()
        wait(100)
        check("restart_loads_history_but_no_old_alerts", other.log_model.rowCount() > 0 and not other.notice.isVisible())
        check("restart_restores_custom_room_name", other.room_name.text() == "新的显示名称")
        other.close()
        deadline = time.monotonic() + 3
        while not other.closed and time.monotonic() < deadline:
            wait(30)
        check("responsive_shutdown", other.closed)
        # Check usable regions at minimum logical size, including a visible fault.
        window.resize(920, 620)
        window.runtime.controller.fail(window.runtime.controller.begin(request), error)
        window.tick()
        window.close_strong_notice()
        window.go_page(3)
        wait(80)
        check("minimum_size_notification_and_table_have_space",
              window.notice.geometry().bottom() < window.stack.geometry().top() and window.log_table.height() >= 80)
        shot("07-minimum-log-notice")
        check("qt_window_clamped_to_available_screen", window.width() <= window.screen().availableGeometry().width())
        from .validate_features import validate_features
        window.resize(min(1240, window.screen().availableGeometry().width() - 40),
                      min(850, window.screen().availableGeometry().height() - 50))
        validate_features(window, check, shot)
        from ..vod import Video, Track, VideoSource
        video = Video("BV17x411w7KC", 170001, "视频名称与长分 P 标题的布局检查", ({"cid": 279786, "page": 1, "duration": 199, "title": "第一集"},))
        vtrack = Track("video:16:avc1:1000", "video", 16, "360P · 512×288 · AVC", "avc1", 1000, ())
        atrack = Track("audio:30216:mp4a:50000", "audio", 30216, "AAC · 50 kbps", "mp4a", 50000, ())
        window.vod_page.input.setText(video.bvid)
        window.vod_page.set_source(VideoSource(video, video.pages[0], (vtrack,), (atrack,), vtrack, atrack))
        window.go_page(5);window.tick();wait(80)
        check("video_track_selectors_and_saved_index", window.vod_page.parts.count() == 1 and window.vod_page.videos.count() == 1 and window.vod_page.audios.count() == 1)
        shot("12-video-page")
        window.resize(920, 620);wait(80)
        check("video_play_controls_visible_at_minimum_size", window.vod_page.play_button.isVisible() and
              window.vod_page.play_button.mapTo(window.vod_page, window.vod_page.play_button.rect().bottomRight()).y() < window.vod_page.height())
        shot("13-video-minimum")
        from ..bangumi import Episode, Season, BangumiSource
        ep = Episode(693247, 43164, 875212290, 944573356, "BV1yW4y1j7Ft", "1", "长剧集名称的布局检查", 1415)
        pgc = window.bangumi_page
        pgc.season = Season(43164, "番剧季度与选集布局检查", (ep,), ({"season_id": 43164, "title": "第一季"},))
        pgc.episode = ep;pgc.populate_episodes(ep.ep_id)
        pgc.title.setText(pgc.season.title);pgc.name.setText("自定义作品名称")
        pgc.seasons.blockSignals(True);pgc.seasons.addItem("第一季", 43164);pgc.seasons.blockSignals(False)
        pgc.videos.addItem("1080P 高码率 · 1920×1080 · AVC", "video:112:avc1:1800000")
        pgc.audios.addItem("AAC · 192 kbps", "audio:30280:mp4a:192000")
        pgc.rights.setText("离线布局样本 · 模拟整集资格，未进行账号或播放验证。")
        window.go_page(6);window.tick();wait(80)
        check("bangumi_controls_visible_at_minimum_size", pgc.play_button.isVisible() and
              pgc.play_button.mapTo(pgc, pgc.play_button.rect().bottomRight()).y() < pgc.height())
        shot("14-bangumi-minimum")
        window.resize(min(1240, window.screen().availableGeometry().width() - 40),
                      min(850, window.screen().availableGeometry().height() - 50));wait(80)
        shot("15-bangumi-page")
        result["success"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        foreground_patch.stop()
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(output, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        window.close()
        deadline = time.monotonic() + 5
        while not window.closed and time.monotonic() < deadline:
            wait(40)
        app.exit(0 if result["success"] else 1)
