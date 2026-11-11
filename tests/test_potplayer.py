import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from live_viewer.core import PlatformClient, Stream, ViewerError
from live_viewer.potplayer import PrivateProfile, PotPlayerAdapter, Win32Player, GET_STATE, read_ini, valid_local_url, write_ini
from live_viewer.relay import LocalRelay, RelayEndpoint


class PrivateProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.installed = self.root / "installed"
        self.installed.mkdir()
        executable = bytearray(256)
        executable[:2] = b"MZ"
        executable[0x3C:0x40] = (128).to_bytes(4, "little")
        executable[128:134] = b"PE\0\0\x64\x86"
        self.executable = self.installed / "PotPlayerMini64.exe"
        self.executable.write_bytes(executable)
        (self.installed / "PotPlayer64.dll").write_bytes(b"SYNTHETIC_DLL_DO_NOT_EXECUTE")
        (self.installed / "PotPlayerMini64.ini").write_text("PERSONAL_SETTINGS_DO_NOT_COPY")
        (self.installed / "Playlist").mkdir()
        (self.installed / "Playlist/default.dpl").write_text("PERSONAL_HISTORY_DO_NOT_COPY")
        self.profiles = []

    def tearDown(self):
        for profile in self.profiles:
            if not profile.closed:
                profile.save_and_clean()
        self.temp.cleanup()

    def profile(self):
        profile = PrivateProfile(self.root / "data", self.executable)
        self.profiles.append(profile)
        return profile

    def test_private_ini_executable_and_playlist_are_separate_from_installation(self):
        profile = self.profile()
        profile.prepare()
        self.assertEqual(profile.executable.name, "LiveViewerPot.exe")
        self.assertEqual(read_ini(profile.ini)["Settings"]["IgnoreErrorMsg"], "1")
        self.assertFalse((profile.path / "PotPlayerMini64.ini").exists())
        self.assertFalse((profile.path / "Playlist/default.dpl").exists())
        self.assertEqual((self.installed / "PotPlayerMini64.ini").read_text(), "PERSONAL_SETTINGS_DO_NOT_COPY")
        private = profile.path
        engine = profile.root / "engines" / profile.engine_id
        profile.save_and_clean()
        self.assertFalse(private.exists())
        self.assertTrue((engine / "PotPlayer64.dll").is_file())
        self.assertTrue(self.executable.is_file())

    def test_two_instances_share_cached_static_files_and_have_distinct_mutable_settings(self):
        first, second = self.profile(), self.profile()
        first.prepare();second.prepare()
        self.assertEqual(first.engine_id, second.engine_id)
        self.assertNotEqual(first.path, second.path)
        parser = read_ini(first.ini)
        parser["Settings"]["AudioVolume"] = "42"
        write_ini(first.ini, parser)
        self.assertNotIn("AudioVolume", read_ini(second.ini)["Settings"])

    def test_preferences_are_restored_without_stale_sources_or_auto_actions(self):
        first = self.profile();first.prepare()
        parser = read_ini(first.ini)
        parser["Settings"].update(AudioVolume="42", LastFileName="http://127.0.0.1:1234/old/live.flv",
                                  LastPlayListName="old-album", ShutdownWhenDone="1", IgnoreErrorMsg="0")
        write_ini(first.ini, parser)
        first.save_and_clean()
        second = self.profile();second.prepare()
        values = read_ini(second.ini)["Settings"]
        self.assertEqual(values["AudioVolume"], "42")
        self.assertEqual(values["LastPlayListName"], "LiveViewer")
        self.assertEqual(values["IgnoreErrorMsg"], "1")
        self.assertNotIn("LastFileName", values)
        self.assertNotIn("ShutdownWhenDone", values)

    def test_cleanup_rejects_paths_outside_managed_profile_directory(self):
        with self.assertRaises(ValueError):
            PrivateProfile._remove_owned(self.installed, self.root / "data/potplayer/runs")
        self.assertTrue(self.executable.exists())

    def test_wrong_architecture_is_rejected_without_running_a_program(self):
        data = bytearray(self.executable.read_bytes())
        data[132:134] = b"\x4c\x01"
        self.executable.write_bytes(data)
        with self.assertRaises(ViewerError) as error:
            self.profile().prepare()
        self.assertEqual(error.exception.code, "player.architecture_unsupported")

    def test_only_loopback_media_addresses_are_accepted(self):
        self.assertEqual(valid_local_url("http://127.0.0.1:12345/token/live.flv"), "http://127.0.0.1:12345/token/live.flv")
        for url in ("https://cdn.invalid/live.flv", "http://user:password@127.0.0.1:1/t/live.flv",
                    "http://127.0.0.1:1/t/live.flv?Cookie=SECRET", "http://localhost:1/t/live.flv"):
            with self.assertRaises(ViewerError):
                valid_local_url(url)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.process = SimpleNamespace(pid=77, _handle=123, poll=lambda: None)
        self.executable = Path("owned-player.exe").resolve()
        self.user, self.kernel = Mock(), Mock()
        self.pid, self.window_class, self.image = 77, "PotPlayer64", str(self.executable)
        def pid(_, value):
            ctypes.cast(value, ctypes.POINTER(W.DWORD)).contents.value = self.pid
            return 1
        def klass(_, buffer, __):
            buffer.value = self.window_class
            return len(self.window_class)
        def image(_, __, buffer, ___):
            buffer.value = self.image
            return True
        self.user.GetWindowThreadProcessId.side_effect = pid
        self.user.GetClassNameW.side_effect = klass
        self.kernel.QueryFullProcessImageNameW.side_effect = image
        self.factory = patch("live_viewer.potplayer.ctypes.WinDLL", side_effect=lambda name, **_: self.user if name == "user32" else self.kernel)
        self.factory.start()
        self.native = Win32Player(self.process, self.executable)
        self.native.hwnd = 100
        def send(*args):
            ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_size_t)).contents.value = 2
            return True
        self.user.SendMessageTimeoutW.side_effect = send

    def tearDown(self):
        self.factory.stop()

    def test_control_never_targets_another_pid_window_class_or_executable(self):
        for key, bad in (("pid", 78), ("window_class", "OtherApp"), ("image", str(Path("other-player.exe").resolve()))):
            previous = getattr(self, key)
            setattr(self, key, bad)
            with self.assertRaises(ViewerError):
                self.native.send(GET_STATE)
            self.user.SendMessageTimeoutW.assert_not_called()
            setattr(self, key, previous)

    def test_process_exit_is_ownership_loss_even_if_a_window_handle_was_reused(self):
        self.process.poll = lambda: 0
        with self.assertRaises(ViewerError):
            self.native.send(GET_STATE)
        self.user.SendMessageTimeoutW.assert_not_called()

    def test_timeouts_are_bounded_and_surface_a_typed_error(self):
        self.user.SendMessageTimeoutW.side_effect = None
        self.user.SendMessageTimeoutW.return_value = False
        with self.assertRaises(ViewerError) as error:
            self.native.send(GET_STATE)
        self.assertEqual(error.exception.code, "player.control_timeout")
        self.assertEqual(self.user.SendMessageTimeoutW.call_args.args[-2], 200)

    def test_stop_between_close_and_reopen_prevents_the_late_play_command(self):
        allowed = [True]
        def stop(*_):
            allowed[0] = False
        self.native.stop = Mock(side_effect=stop)
        self.native.reopen(lambda: allowed[0])
        self.user.SendMessageTimeoutW.assert_not_called()


class ActorTests(unittest.TestCase):
    def test_initial_query_timeouts_retry_without_a_false_control_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            actor = PotPlayerAdapter(temporary, Path(temporary) / "synthetic.exe")
            process = SimpleNamespace(poll=lambda: None, wait=lambda timeout: 0)
            native = Mock()
            native.hwnd = 100
            native.find.return_value = True
            native.snapshot.side_effect = [
                ViewerError("加载中超时", code="player.control_timeout"),
                ViewerError("加载中超时", code="player.control_timeout"),
                {"state": "playing", "owned_source": True, "source_present": True},
            ]
            native.visible_dialog_count.return_value = 0
            native.close.side_effect = lambda: setattr(process, "poll", lambda: 0)
            actor.process, actor.native = process, native
            actor._open = Mock()
            try:
                actor.open("http://127.0.0.1:12345/test/live.flv")
                deadline = time.monotonic() + 2
                while actor.snapshot()["state"] != "playing" and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(actor.snapshot()["state"], "playing")
                self.assertFalse(any(e["kind"] == "error" for e in actor.poll()))
            finally:
                actor.close(wait=True)

    def test_pause_command_timeout_is_not_hidden_by_loading_grace(self):
        with tempfile.TemporaryDirectory() as temporary:
            actor = PotPlayerAdapter(temporary, Path(temporary) / "synthetic.exe")
            process = SimpleNamespace(poll=lambda: None, wait=lambda timeout: 0)
            native = Mock()
            native.hwnd = 100
            native.find.return_value = True
            native.snapshot.return_value = {"state": "ended", "owned_source": True, "source_present": True}
            native.send.side_effect = ViewerError("控制超时", code="player.control_timeout")
            native.visible_dialog_count.return_value = 0
            native.close.side_effect = lambda: setattr(process, "poll", lambda: 0)
            actor.process, actor.native = process, native
            actor._open = Mock()
            try:
                actor.open("http://127.0.0.1:12345/test/live.flv")
                actor.pause()
                events, deadline = [], time.monotonic() + 2
                while not events and time.monotonic() < deadline:
                    events.extend(e for e in actor.poll() if e["kind"] == "error")
                    time.sleep(.01)
                self.assertEqual(events[0]["error"].code, "player.control_timeout")
            finally:
                actor.close(wait=True)

    def test_query_timeout_after_loading_grace_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            actor = PotPlayerAdapter(temporary, Path(temporary) / "synthetic.exe")
            process = SimpleNamespace(poll=lambda: None, wait=lambda timeout: 0)
            native = Mock()
            native.hwnd = 100
            native.find.return_value = True
            queried = threading.Event()
            def snapshot(_):
                queried.set()
                raise ViewerError("查询超时", code="player.control_timeout")
            native.snapshot.side_effect = snapshot
            native.close.side_effect = lambda: setattr(process, "poll", lambda: 0)
            actor.process, actor.native = process, native
            actor._open = Mock()
            clock = [0]
            try:
                with patch("live_viewer.potplayer.time", SimpleNamespace(monotonic=lambda: clock[0])):
                    actor.open("http://127.0.0.1:12345/test/live.flv")
                    self.assertTrue(queried.wait(1))
                    clock[0] = 6
                    events, deadline = [], time.monotonic() + 2
                    while not events and time.monotonic() < deadline:
                        events.extend(e for e in actor.poll() if e["kind"] == "error")
                        time.sleep(.01)
                    self.assertEqual(events[0]["error"].code, "player.control_timeout")
            finally:
                actor.close(wait=True)

    def test_query_timeout_after_loading_grace_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            actor = PotPlayerAdapter(temporary, Path(temporary) / "synthetic.exe")
            process = SimpleNamespace(poll=lambda: None, wait=lambda timeout: 0)
            native = Mock()
            native.hwnd = 100
            native.find.return_value = True
            queried = threading.Event()
            def snapshot(_):
                queried.set()
                raise ViewerError("查询超时", code="player.control_timeout")
            native.snapshot.side_effect = snapshot
            native.close.side_effect = lambda: setattr(process, "poll", lambda: 0)
            actor.process, actor.native = process, native
            actor._open = Mock()
            clock = [0]
            try:
                with patch("live_viewer.potplayer.time", SimpleNamespace(monotonic=lambda: clock[0])):
                    actor.open("http://127.0.0.1:12345/test/live.flv")
                    self.assertTrue(queried.wait(1))
                    clock[0] = 6
                    events, deadline = [], time.monotonic() + 2
                    while not events and time.monotonic() < deadline:
                        events.extend(e for e in actor.poll() if e["kind"] == "error")
                        time.sleep(.01)
                    self.assertEqual(events[0]["error"].code, "player.control_timeout")
            finally:
                actor.close(wait=True)

    def test_stop_during_a_pending_open_invalidates_guard_and_does_not_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            actor = PotPlayerAdapter(temporary, Path(temporary) / "synthetic.exe")
            entered, release = threading.Event(), threading.Event()
            process = SimpleNamespace(poll=lambda: None, wait=lambda timeout: 0)
            native = Mock()
            native.hwnd = 100;native.find.return_value = True
            def snapshot(_):
                entered.set()
                release.wait(2)
                return {"state": "playing", "owned_source": True, "source_present": True}
            native.snapshot.side_effect = snapshot
            native.visible_dialog_count.return_value = 0
            def close():
                process.poll = lambda: 0
            native.close.side_effect = close
            allowed = [True]
            actor.process, actor.native, actor.ever_launched = process, native, True
            try:
                actor.open("http://127.0.0.1:12345/test/live.flv", guard=lambda: allowed[0])
                self.assertTrue(entered.wait(2))
                allowed[0] = False
                actor.stop()
                release.set()
                deadline = time.monotonic() + 2
                while not native.stop.called and time.monotonic() < deadline:
                    time.sleep(.01)
                native.reopen.assert_not_called()
                self.assertEqual(actor.intent, "stopped")
            finally:
                release.set()
                actor.close(wait=True)


class StableRelayTests(unittest.TestCase):
    def test_replacement_keeps_address_and_delivers_distinct_complete_streams(self):
        endpoint = RelayEndpoint()
        relays = []
        class Response:
            def __init__(self, content): self.content = content
            def iter_content(self, _): return iter([self.content])
            def close(self): pass
        try:
            address = None
            for payload in (b"FLV-FIRST-COMPLETE", b"FLV-SECOND-COMPLETE", b"FLV-THIRD-COMPLETE"):
                client = PlatformClient("douyu")
                stream = Stream("douyu", "6657", 0, 0, "原画", [], ["https://test.douyucdn.cn/live"])
                relay = LocalRelay("douyu", "6657", None, initial_client=client, initial_stream=stream,
                                   endpoint=endpoint, low_latency=False)
                relays.append(relay)
                with patch("live_viewer.relay.open_media", return_value=Response(payload)):
                    relay.prepare_first_source()
                    relay.start()
                    session = requests.Session();session.trust_env = False
                    try:
                        response = session.get(relay.url, timeout=3)
                        self.assertEqual(response.content, payload)
                        address = address or relay.url
                        self.assertEqual(relay.url, address)
                    finally:
                        session.close()
                relay.stop()
                session = requests.Session();session.trust_env = False
                try:
                    self.assertEqual(session.get(address, timeout=3).status_code, 503)
                finally:
                    session.close()
        finally:
            for relay in relays:
                relay.stop()
            endpoint.close()


if __name__ == "__main__":
    unittest.main()
