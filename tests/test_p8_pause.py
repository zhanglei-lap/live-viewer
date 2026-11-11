from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from live_viewer.potplayer import PotPlayerAdapter, SET_STATE


class StartupPauseTests(unittest.TestCase):
    def test_delayed_startup_seek_cannot_resume_requested_pause_and_user_resume_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("live_viewer.potplayer.process_memory", return_value={}):
                player = PotPlayerAdapter(directory, Path(directory)/"unused.exe")
                player._open = Mock()
                player.profile.save_and_clean = Mock()
                player.profile.playlist_summary = Mock(return_value={})
                process = Mock();process.poll.return_value = None
                process.wait.side_effect = lambda **kwargs: setattr(process.poll, "return_value", 0)
                native = Mock();native.find.return_value = True;native.visible_dialog_count.return_value = 0
                state = {"state": "playing", "position_seconds": 0, "owned_source": True, "width": 1920, "height": 1080}
                lock = threading.Lock()
                def snapshot(*a):
                    with lock:return dict(state)
                def send(code, value, **kw):
                    if code == SET_STATE:
                        with lock:state["state"] = "paused" if value == 1 else "playing"
                native.snapshot.side_effect = snapshot;native.send.side_effect = send
                player.native, player.process = native, process
                try:
                    player.open("http://127.0.0.1:12345/test/vod-video.mp4", audio_url="http://127.0.0.1:12345/test/vod-audio.m4a",
                                start_position=30, start_paused=True)
                    end = time.monotonic()+2
                    while time.monotonic()<end and state["state"]!="paused":time.sleep(.01)
                    self.assertEqual(state["state"], "paused")
                    with lock:state.update(state="playing", position_seconds=30)
                    end = time.monotonic()+2
                    while time.monotonic()<end and state["state"]!="paused":time.sleep(.01)
                    self.assertEqual(state["state"], "paused")
                    player.resume()
                    end = time.monotonic()+2
                    while time.monotonic()<end and state["state"]!="playing":time.sleep(.01)
                    self.assertEqual(state["state"], "playing")
                finally:player.close(wait=True)
