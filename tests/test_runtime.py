import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from live_viewer.core import ViewerError
from live_viewer.events import EventCenter
from live_viewer.runtime import LiveRuntime
from live_viewer.settings import sanitize_settings


class FakePlayer:
    def __init__(self, directory, path):
        self.profile = SimpleNamespace(source=path)
        self.ever_launched = False
        self.serial = 0
        self.opens = self.stops = 0
        self.data = {"state": "idle"}
        self.allow = []

    def snapshot(self):
        return dict(self.data)

    def prepare(self, **kwargs):
        pass

    def open(self, *args, **kw):
        self.last_open_options = dict(kw)
        if not kw["guard"]():
            raise ViewerError("已取消", code="playback.cancelled")
        self.serial += 1
        self.opens += 1
        self.allow.append(kw["allow_new_process"])
        self.ever_launched = True
        self.data = {"state": "loading", "load_serial": self.serial}
        return self.serial

    def poll(self):
        return []

    def stop(self):
        self.stops += 1
        self.data["state"] = "closed_media"

    def pause(self):
        self.data["state"] = "paused"

    def resume(self):
        self.data["state"] = "playing"

    def seek(self, seconds):
        self.data["position_seconds"] = seconds

    def close(self, **kw):
        self.data["state"] = "closed"


class FakeRelay:
    def __init__(self):
        self.stop_event = threading.Event()
        self.started = False
        self.url = "http://127.0.0.1:12345/private/live.flv"
        self.bytes = 0
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stop_event.set()
        self.stopped = True

    def snapshot(self):
        return {"bytes_forwarded": self.bytes, "connections": 1, "failures": 0}


class FakeService:
    def __init__(self, controller):
        self.controller = controller
        self.gate = None
        self.created = []
        self.error = None

    def prepare(self, *args, **kw):
        if self.gate:
            self.gate.wait(3)
        if self.error:
            raise self.error
        relay = FakeRelay()
        self.created.append(relay)
        if args[0].get("kind") == "vod":
            from vod_fixtures import source
            relay.audio_url = "http://127.0.0.1:12345/private/vod-audio.m4a"
            return relay, source()
        return relay, SimpleNamespace(actual_name="1080P", qualities=[])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        path = self.directory / "fake.exe"
        path.write_bytes(b"test fixture, never executed")
        self.config = sanitize_settings({"player": str(path)})
        self.events = EventCenter(self.directory / "logs")
        self.persisted = 0
        def persist():
            self.persisted += 1
        self.runtime = LiveRuntime(self.events, self.directory, self.config, {}, persist=persist,
                                   player_factory=FakePlayer, service_factory=FakeService)
        self.request = {"platform": "douyu", "room_input": "6657", "room_name": "测试房间",
                        "requested": None, "session": "匿名", "backend": "potplayer",
                        "network_mode": "direct", "low_latency": True, "replay_mode": False}

    def tearDown(self):
        if self.runtime.service.gate:
            self.runtime.service.gate.set()
        self.runtime.close()
        self.events.close()
        self.temp.cleanup()

    def until(self, condition):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.runtime.tick()
            if condition():
                return
            time.sleep(.01)
        self.fail("condition timeout")

    def started(self):
        self.assertTrue(self.runtime.play(self.request))
        self.until(lambda: self.runtime.relay is not None)
        return self.runtime.player

    def playing(self, player, position=1):
        player.data.update(state="playing", position_seconds=position, width=1920, height=1080,
                           source_present=True, owned_source=True)
        self.runtime.tick()

    def test_stop_rejects_late_resolution_and_disposes_relay(self):
        self.runtime.service.gate = threading.Event()
        self.runtime.play(self.request)
        self.runtime.stop()
        self.runtime.service.gate.set()
        self.until(lambda: bool(self.runtime.service.created) and self.runtime.service.created[0].stopped)
        self.assertEqual(self.runtime.player.opens, 0)
        self.assertEqual(self.runtime.controller.state, "stopped")

    def test_external_close_wins_over_queued_preparation(self):
        player = self.started()
        self.playing(player)
        self.playing(player, 2)
        self.runtime.service.gate = threading.Event()
        self.runtime.reconnect()
        self.runtime.service.gate.set()
        time.sleep(.05)  # Result is queued but has not been accepted by the GUI.
        player.data["state"] = "closed"
        self.runtime.tick()
        self.assertIsNone(self.runtime.active)
        self.assertEqual(player.opens, 1)
        self.assertFalse(self.runtime.controller.retry.active)

    def test_tool_pause_then_continue_accepts_already_paused_player(self):
        player = self.started()
        self.playing(player)
        self.runtime.pause()
        self.assertEqual(self.runtime.controller.state, "paused")
        self.runtime.pause()
        self.until(lambda: player.opens == 2)
        self.assertFalse(self.runtime.paused)

    def test_external_pause_during_preparation_cancels_late_open(self):
        player = self.started()
        self.playing(player)
        self.runtime.service.gate = threading.Event()
        self.runtime.reconnect()
        player.data["state"] = "paused"
        self.runtime.tick()
        self.runtime.service.gate.set()
        self.until(lambda: len(self.runtime.service.created) == 2)
        self.assertTrue(self.runtime.paused)
        self.assertEqual(player.opens, 1)

    def test_external_close_media_cancels_retry(self):
        player = self.started()
        self.playing(player)
        player.data["state"] = "closed_media"
        self.runtime.tick()
        self.assertIsNone(self.runtime.active)
        self.assertFalse(self.runtime.controller.retry.active)

    def test_another_movie_does_not_get_stopped_or_reopened(self):
        player = self.started()
        player.data.update(source_present=True, owned_source=False)
        self.runtime.tick()
        self.assertIsNone(self.runtime.active)
        self.assertEqual(player.stops, 0)
        self.assertEqual(self.events.notice().code, "player.source_changed")

    def test_reconnect_uses_active_room_and_disposes_old_relay(self):
        player = self.started()
        old = self.runtime.relay
        self.runtime.reconnect()
        self.until(lambda: player.opens == 2)
        self.assertTrue(old.stop_event.is_set())
        self.assertEqual(self.runtime.active["room_input"], "6657")

    def test_history_recorded_only_after_transport_and_only_once(self):
        self.started()
        self.assertEqual(self.config["history"], [])
        self.runtime.relay.bytes = 100
        self.runtime.tick()
        self.runtime.tick()
        self.assertEqual(self.persisted, 1)
        self.assertEqual(self.config["history"][0]["room"], "6657")

    def test_offline_does_not_retry_and_is_warning(self):
        self.runtime.service.error = ViewerError("未开播", code="room.offline", level="WARN")
        self.runtime.play(self.request)
        self.until(lambda: not self.runtime.busy)
        self.assertEqual(self.runtime.controller.state, "unavailable")
        self.assertFalse(self.runtime.controller.retry.active)
        self.assertEqual(self.events.notice().level, "WARN")

    def test_preflight_network_failure_retries_without_native_graph(self):
        self.runtime.service.error = ViewerError("连接失败", code="network.connection_failed")
        self.runtime.play(self.request)
        self.until(lambda: not self.runtime.busy)
        retry = self.runtime.controller.retry
        self.assertIsNotNone(retry.due)
        self.runtime.service.error = None
        retry.due = time.monotonic() - 1
        self.until(lambda: self.runtime.relay is not None)
        self.assertEqual(retry.total_attempts, 1)

    def test_dimensions_and_progress_required_before_playing(self):
        player = self.started()
        self.playing(player, 1)
        self.assertEqual(self.runtime.controller.state, "connecting")
        self.playing(player, 2)
        self.assertEqual(self.runtime.controller.state, "playing")

    def test_report_after_stop_preserves_transport_but_never_claims_decoded_picture(self):
        player = self.started()
        self.playing(player)
        self.runtime.relay.bytes = 100
        self.runtime.stop()
        report = self.runtime.report(confirmed=True, note="Cookie: private-secret")
        self.assertEqual(report["state"], "stopped")
        self.assertEqual(report["transport"]["bytes_forwarded"], 100)
        self.assertFalse(report["decoded_verified"])
        self.assertTrue(report["picture_and_audio_confirmed_by_user"])
        self.assertNotIn("private-secret", report["user_note"])

    def test_disabled_recovery_never_claims_to_be_reconnecting(self):
        self.runtime.set_auto_reconnect(False)
        player = self.started()
        self.runtime.controller.relay_event(self.runtime.token, "relay.interrupted", "已断流")
        self.runtime.tick()
        self.assertEqual(self.runtime.controller.state, "error")
        self.assertFalse(self.runtime.controller.retry.active)
        self.assertIsNone(self.runtime.controller.retry.due)
        self.assertEqual(player.opens, 1)

    def test_reenabling_recovery_resumes_network_failure_but_not_user_stop(self):
        self.runtime.set_auto_reconnect(False)
        player = self.started()
        self.runtime.controller.fail(self.runtime.token, ViewerError("网络失败", code="network.timeout"))
        self.runtime.set_auto_reconnect(True)
        self.assertIsNotNone(self.runtime.controller.retry.due)
        self.runtime.controller.retry.due = time.monotonic() - 1
        self.until(lambda: player.opens == 2)
        self.runtime.stop()
        self.runtime.set_auto_reconnect(False)
        self.runtime.set_auto_reconnect(True)
        self.assertFalse(self.runtime.controller.retry.active)

    def test_auto_recovery_option_does_not_override_a_control_failure(self):
        self.started()
        self.runtime.controller.fail(self.runtime.token, ViewerError("控制超时", code="player.control_timeout"))
        self.runtime.set_auto_reconnect(False)
        self.runtime.set_auto_reconnect(True)
        self.assertFalse(self.runtime.controller.retry.active)
        self.assertIsNone(self.runtime.controller.retry.due)

    def test_native_replay_continue_keeps_same_item_and_position(self):
        self.request["replay_mode"] = True
        player = self.started()
        self.playing(player, 20)
        self.runtime.pause()
        self.runtime.pause()
        self.runtime.tick()
        self.assertEqual(player.opens, 1)
        self.assertEqual(player.data["position_seconds"], 20)
        self.assertFalse(self.runtime.paused)

    def test_native_live_edge_uses_seek_without_replacing_history(self):
        self.request["replay_mode"] = True
        player = self.started()
        self.playing(player, 20)
        player.data["duration_seconds"] = 60
        self.runtime.reconnect()
        self.assertEqual(player.opens, 1)
        self.assertEqual(player.data["position_seconds"], 58)
        self.assertEqual(self.runtime.active["room_input"], "6657")

    def test_old_cached_picture_progress_does_not_clear_upstream_failure(self):
        player = self.started()
        self.playing(player, 1)
        self.runtime.controller.relay_event(self.runtime.token, "relay.interrupted", "输入已中断")
        self.playing(player, 2)
        self.assertEqual(self.events.notice_for("playback").code, "relay.interrupted")
        self.assertEqual(self.runtime.controller.state, "reconnecting")
        self.assertIsNotNone(self.runtime.controller.retry.due)

    def test_stop_during_native_replacement_disposes_result_and_allows_next_play(self):
        self.request["replay_mode"] = True
        old = self.started()
        self.playing(old, 1)
        self.playing(old, 2)
        gate = threading.Event()
        close = old.close
        def delayed_close(**kwargs):
            gate.wait(2)
            close(**kwargs)
        old.close = delayed_close
        created = []
        def factory(*args):
            player = FakePlayer(*args)
            created.append(player)
            return player
        self.runtime.player_factory = factory
        try:
            self.assertTrue(self.runtime.play(self.request, automatic=True))
            self.until(lambda: self.runtime.replacing_player)
            pending = self.runtime.service.created[-1]
            self.runtime.stop()
            gate.set()
            self.until(lambda: bool(created) and created[0].data["state"] == "closed" and pending.stopped)
            self.assertFalse(self.runtime.replacing_player)
            self.assertIsNone(self.runtime.active)
            self.assertEqual(created[0].opens, 0)
            self.assertTrue(self.runtime.play(self.request))
            self.until(lambda: self.runtime.relay is not None)
            self.playing(self.runtime.player, 10)
            self.playing(self.runtime.player, 11)
            self.assertEqual(self.runtime.controller.state, "playing")
        finally:
            gate.set()

    def test_cancel_account_job_cannot_restore_removed_login(self):
        gate = threading.Event()
        accepted = []
        self.runtime.jobs.submit("account.douyu", lambda: (gate.wait(1), "cookie"), accepted.append, lambda _: None)
        self.runtime.jobs.cancel("account.douyu")
        gate.set()
        time.sleep(.04)
        self.runtime.tick()
        self.assertEqual(accepted, [])

    def video_request(self):
        self.request.update(kind="vod", room_input="BV17x411w7KC:P1", video_input="BV17x411w7KC",
                            bvid="BV17x411w7KC", cid=279786, page=1, replay_mode=False)

    def test_vod_pause_continue_does_not_reopen_or_jump_to_start(self):
        self.video_request();player = self.started()
        self.playing(player, 20);self.runtime.pause();self.runtime.pause()
        self.runtime.tick()
        self.assertEqual(player.opens, 1)
        self.assertEqual(player.data["position_seconds"], 20)
        self.assertIsNone(self.runtime.controller.retry.due)

    def test_vod_natural_completion_never_autoreconnects(self):
        self.video_request();player = self.started()
        self.playing(player, 198)
        player.data.update(state="ended", duration_seconds=199, position_seconds=199)
        self.runtime.tick()
        self.assertEqual(self.runtime.controller.state, "ended")
        self.assertIsNone(self.runtime.controller.retry.due)
        self.assertEqual(player.opens, 1)

    def test_vod_early_end_is_an_error_and_recovery_setting_does_not_restart_it(self):
        self.video_request();player = self.started()
        self.playing(player, 20)
        player.data.update(state="ended", duration_seconds=199, position_seconds=21)
        self.runtime.tick();self.runtime.set_auto_reconnect(True)
        self.assertEqual(self.runtime.controller.state, "error")
        self.assertEqual(self.events.notice().code, "vod.ended_early")
        self.assertFalse(self.runtime.controller.retry.active)

    def test_vod_buffered_progress_does_not_hide_failed_audio_input(self):
        self.video_request();player = self.started()
        self.playing(player, 20)
        self.runtime.controller.relay_event(self.runtime.token, "vod.url_expired", "音轨已失效", level="ERROR", impact="interrupted", presentation="strong")
        self.playing(player, 21)
        self.assertEqual(self.runtime.controller.state, "reconnecting")
        self.assertEqual(self.events.notice().code, "vod.url_expired")

    def test_switching_vod_to_http_live_uses_a_new_owned_player(self):
        self.video_request();old = self.started()
        self.runtime.stop()
        self.request.pop("kind");self.request["room_input"] = "6657"
        self.assertTrue(self.runtime.play(self.request))
        self.until(lambda: self.runtime.relay is not None)
        self.assertIsNot(self.runtime.player, old)
        self.assertEqual(self.runtime.player.opens, 1)

    def test_video_history_uses_content_ids_and_never_changes_room_history(self):
        self.video_request();self.started()
        self.runtime.relay.bytes = 100;self.runtime.tick()
        self.assertEqual(self.config["video_history"][0]["cid"], 279786)
        self.assertEqual(self.config["history"], [])
        self.assertTrue(self.runtime.token.target.startswith("vod:"))

    def test_pause_and_manual_video_recovery_restore_time_and_pause(self):
        self.video_request();old = self.started()
        old.data["duration_seconds"] = 199;self.playing(old, 65)
        self.runtime.pause();self.runtime.tick()
        self.runtime.reconnect();self.until(lambda: self.runtime.player is not old and self.runtime.relay is not None)
        options = self.runtime.player.last_open_options
        self.assertEqual(options["start_position"], 65);self.assertTrue(options["start_paused"])

    def test_early_end_bookmark_is_not_marked_completed(self):
        self.video_request();player = self.started()
        player.data["duration_seconds"] = 199;self.playing(player, 40)
        player.data.update(state="ended", position_seconds=41);self.runtime.tick()
        self.runtime.stop()
        record = self.config["video_progress"]["BV17x411w7KC:279786"]
        self.assertFalse(record["completed"]);self.assertEqual(record["position"], 41)

    def test_loading_near_zero_cannot_overwrite_pending_resume(self):
        self.video_request();self.request["resume_position"] = 65
        self.config["video_progress"]["BV17x411w7KC:279786"] = {"position": 65, "duration": 199, "page": 1}
        player = self.started();player.data["duration_seconds"] = 199
        self.playing(player, 1);self.playing(player, 2)
        self.assertEqual(self.config["video_progress"]["BV17x411w7KC:279786"]["position"], 65)
        self.runtime.restore_deadline = time.monotonic() - 1;self.runtime.tick()
        self.playing(player, 3)
        self.assertEqual(self.events.notice().code, "vod.resume_failed")
        self.assertEqual(self.config["video_progress"]["BV17x411w7KC:279786"]["position"], 65)

    def test_video_automatic_recovery_reopens_at_previous_position(self):
        self.video_request();old = self.started();old.data["duration_seconds"] = 199
        self.playing(old, 60);self.playing(old, 61)
        self.runtime.controller.relay_event(self.runtime.token, "vod.network_failed", "网络断开", level="ERROR", impact="interrupted")
        self.runtime.tick();self.runtime.controller.retry.due = time.monotonic() - 1
        self.until(lambda: self.runtime.player is not old and self.runtime.relay is not None)
        self.assertEqual(self.runtime.player.last_open_options["start_position"], 61)
        self.assertFalse(self.runtime.player.last_open_options["start_paused"])

    def test_stop_cancels_late_video_automatic_recovery(self):
        self.video_request();old = self.started();old.data["duration_seconds"] = 199;self.playing(old, 60)
        self.runtime.service.gate = threading.Event()
        self.runtime.play(self.request, automatic=True)
        self.runtime.stop();self.runtime.service.gate.set()
        self.until(lambda: len(self.runtime.service.created) == 2 and self.runtime.service.created[-1].stopped)
        self.assertIs(self.runtime.player, old);self.assertEqual(old.opens, 1)

    def test_video_recovery_is_bounded_after_three_failed_attempts(self):
        self.video_request();self.started()
        self.runtime.controller.state = "reconnecting"
        self.runtime.controller.retry.total_attempts = 3
        self.runtime._schedule_retry()
        self.assertEqual(self.runtime.controller.state, "error")
        self.assertEqual(self.events.notice().code, "vod.recovery_exhausted")
        self.assertFalse(self.runtime.controller.retry.active)

    def test_account_and_log_errors_retain_their_own_source_and_scope(self):
        error = ViewerError("失败", code="operation.failed")
        self.runtime.error(error, scope="account.bilibili")
        self.runtime.error(error, scope="log.export")
        account = self.events.notice_for("account.bilibili")
        log = self.events.notice_for("log.export")
        self.assertEqual(account.source, "account")
        self.assertEqual(log.source, "log")
        self.assertEqual(account.impact, "degraded")

