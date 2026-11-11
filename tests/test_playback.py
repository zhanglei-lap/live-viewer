import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from live_viewer.core import Stream, ViewerError
from live_viewer.events import EventCenter
from live_viewer.playback import OperationController, PlaybackController, PlaybackService


def request(room="6657"):
    return {"platform": "douyu", "room_input": room, "requested": None, "room_name": "测试房间",
            "session": "匿名", "backend": "potplayer", "network_mode": "direct", "low_latency": True}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = EventCenter(Path(self.temp.name))
        self.controller = PlaybackController(self.events)

    def tearDown(self):
        self.events.close()
        self.temp.cleanup()

    def test_login_success_does_not_clear_playback_failure(self):
        token = self.controller.begin(request())
        self.controller.fail(token, ViewerError("证书连接失败。", code="network.tls_failed"))
        original = self.events.notice()
        self.events.emit("account.imported", "登录成功", source="account", target="bilibili")
        self.assertEqual(self.events.notice(), original)
        self.assertEqual(self.controller.state, "error")

    def test_stop_and_switch_reject_old_failures_without_changing_current_state_or_notices(self):
        previous = self.controller.begin(request())
        self.controller.stop()
        self.assertFalse(self.controller.fail(previous, ViewerError("迟到错误")))
        self.assertEqual(self.controller.state, "stopped")
        self.assertIsNone(self.events.notice())
        current = self.controller.begin(request("6658"))
        self.assertFalse(self.controller.publish(previous, "relay.forwarding", "旧任务成功", state="playing"))
        self.assertEqual(self.controller.snapshot()["target"], current.target)
        self.assertEqual(self.controller.state, "resolving")
        self.assertEqual(self.events.store.read()[-1]["level"], "DEBUG")

    def test_retry_retains_session_changes_generation_and_deduplicates_incident(self):
        token = self.controller.begin(request())
        for attempt in range(3):
            self.controller.fail(token, ViewerError("连接失败。"), recovering=True)
            self.events.drain()
            previous = token
            self.controller.retry.total_attempts += 1
            token = self.controller.begin(request(), automatic=True)
            self.assertEqual(token.session_id, previous.session_id)
            self.assertNotEqual(token.generation, previous.generation)
            self.assertFalse(self.controller.current(previous))
        self.controller.fail(token, ViewerError("最后一次连接失败。"), recovering=True)
        self.assertEqual(self.events.drain()[1], [])
        rows = self.events.store.read()
        self.assertEqual(sum(e["code"] == "playback.retry_started" for e in rows), 3)
        self.controller.relay_event(token, "relay.forwarding", "正在转发")
        self.assertIsNone(self.events.notice())
        self.assertEqual(self.controller.state, "connecting")
        self.controller.fail(token, ViewerError("新一轮断流"), recovering=True)
        self.assertEqual(len(self.events.drain()[1]), 1)

    def test_automatic_retry_is_rejected_after_stop_pause_or_target_change(self):
        self.controller.begin(request())
        self.controller.pause()
        with self.assertRaises(ViewerError):
            self.controller.begin(request(), automatic=True)
        self.controller.stop()
        with self.assertRaises(ViewerError):
            self.controller.begin(request(), automatic=True)
        self.controller.begin(request("6658"))
        with self.assertRaises(ViewerError):
            self.controller.begin(request(), automatic=True)

    def test_offline_is_warn_with_sticky_notice_without_strong_popup(self):
        token = self.controller.begin(request())
        self.controller.fail(token, ViewerError("未开播", code="room.offline", level="WARN"))
        self.assertEqual(self.events.notice().impact, "start_blocked")
        self.assertEqual(self.events.notice().level, "WARN")
        self.assertEqual(self.controller.state, "unavailable")
        self.assertEqual(self.events.drain()[1], [])

    def test_historical_errors_do_not_raise_current_notices_on_restart(self):
        token = self.controller.begin(request())
        self.controller.fail(token, ViewerError("旧故障"))
        self.events.close()
        reopened = EventCenter(Path(self.temp.name))
        try:
            self.assertGreater(len(reopened.store.read()), 0)
            self.assertIsNone(reopened.notice())
            self.assertEqual(reopened.drain(), ([], []))
        finally:
            reopened.close()

    def test_cancel_during_resolve_closes_client_without_creating_relay(self):
        token = self.controller.begin(request())
        client = Mock()
        def resolve(*_):
            self.controller.stop()
            return Stream("douyu", "6657", 0, 0, "原画", [], [])
        client.resolve.side_effect = resolve
        factory = Mock()
        service = PlaybackService(self.controller, client_factory=lambda *a, **k: client, relay_factory=factory)
        with self.assertRaises(ViewerError):
            service.prepare(request(), None, token)
        client.close.assert_called_once()
        factory.assert_not_called()

    def test_cancel_after_source_preflight_disposes_relay_and_does_not_start_server(self):
        token = self.controller.begin(request())
        client, relay = Mock(), Mock()
        client.resolve.return_value = Stream("douyu", "6657", 0, 0, "原画", [], [])
        relay.prepare_first_source.side_effect = self.controller.stop
        service = PlaybackService(self.controller, client_factory=lambda *a, **k: client,
                                  relay_factory=lambda *a, **k: relay)
        with self.assertRaises(ViewerError):
            service.prepare(request(), None, token)
        relay.stop.assert_called_once()
        relay.start.assert_not_called()

    def test_operations_invalidate_by_channel_without_cancelling_other_platform_tasks(self):
        operations = OperationController()
        login = operations.begin("account", "bilibili")
        check = operations.begin("diagnostic", "douyu:6657")
        operations.cancel("account")
        self.assertFalse(operations.current(login))
        self.assertTrue(operations.current(check))
        newer = operations.begin("diagnostic", "bilibili:35")
        self.assertFalse(operations.current(check))
        self.assertTrue(operations.current(newer))
        operations.close()
        self.assertFalse(operations.current(newer))


if __name__ == "__main__":
    unittest.main()
