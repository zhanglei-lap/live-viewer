import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from live_viewer.core import ViewerError
from live_viewer.credentials import CredentialStore
from live_viewer.settings import StateStore, atomic_write, data_directory, remember_play, sanitize_settings


class SettingsTests(unittest.TestCase):
    def test_saved_room_name_is_canonical_for_history_and_last_room(self):
        result = sanitize_settings({"favorites": [{"platform": "douyu", "room": "6657", "name": "玩机器"}],
            "history": [{"platform": "douyu", "room": "6657", "name": "old", "played_at": "today"}],
            "last_room": {"platform": "douyu", "room": "6657", "name": "stale"}})
        self.assertEqual(result["history"][0]["name"], "玩机器")
        self.assertEqual(result["last_room"]["name"], "玩机器")

    def test_legacy_settings_keep_favorites_and_player_without_private_fields(self):
        old = {"favorites": [{"platform": "douyu", "room": "06657", "name": "玩机器"}],
               "player": r"D:\potplayer\PotPlayerMini64.exe", "Cookie": "SECRET", "url": "https://example.com/?SECRET"}
        result = sanitize_settings(old)
        self.assertEqual(result["favorites"][0]["room"], "6657")
        self.assertEqual(result["player"], old["player"])
        self.assertEqual(result["version"], 2)
        self.assertFalse(any(result["remember_login"].values()))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_settings_filter_duplicate_rooms_invalid_preferences_and_unknown_fields(self):
        result = sanitize_settings({"favorites": [None, {"platform": "douyu", "room": "0"},
            {"platform": "douyu", "room": "6657", "name": "first"},
            {"platform": "douyu", "room": "6657", "name": "duplicate"}],
            "room_preferences": {"douyu:6657": {"quality": 4, "mode": "导入 Cookie", "Cookie": "SECRET"},
                                 "evil:35": {"quality": 1}, "bilibili:35": {"quality": True}},
            "remember_login": {"douyu": "true", "bilibili": True}})
        self.assertEqual(len(result["favorites"]), 1)
        self.assertEqual(result["room_preferences"]["douyu:6657"]["quality"], 4)
        self.assertIsNone(result["room_preferences"]["bilibili:35"]["quality"])
        self.assertNotIn("evil:35", result["room_preferences"])
        self.assertFalse(result["remember_login"]["douyu"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_history_moves_repeated_room_to_top_and_is_bounded(self):
        history = []
        for number in range(1, 41):
            history = remember_play(history, {"platform": "douyu", "room": str(number), "name": str(number)})
        history = remember_play(history, {"platform": "douyu", "room": "25", "name": "renamed"})
        self.assertEqual(len(history), 30)
        self.assertEqual(history[0]["room"], "25")
        self.assertEqual(history[0]["name"], "renamed")
        self.assertEqual(sum(row["room"] == "25" for row in history), 1)

    def test_corrupt_settings_are_backed_up_before_replacing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            store.path.write_bytes(b"broken JSON")
            settings = store.load()
            self.assertTrue(store.warning)
            store.save(settings)
            backups = list(Path(directory).glob("settings-unreadable-*.bak"))
            self.assertEqual(backups[0].read_bytes(), b"broken JSON")
            self.assertEqual(store.load(), settings)

    def test_failed_atomic_replace_keeps_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_bytes(b"original")
            with patch.object(Path, "replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError): atomic_write(path, b"new")
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_frozen_settings_are_next_to_executable_not_extraction_directory(self):
        with patch("live_viewer.settings.sys.frozen", True, create=True), \
             patch("live_viewer.settings.sys.executable", str(Path.cwd() / "dist" / "LiveViewer.exe")):
            self.assertEqual(data_directory(), Path.cwd() / "dist" / "data")


@unittest.skipUnless(os.name == "nt", "Windows DPAPI required")
class CredentialTests(unittest.TestCase):
    def test_windows_encryption_roundtrip_domain_filter_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = CredentialStore(Path(directory))
            jar = requests.cookies.RequestsCookieJar()
            jar.set("login_test", "SYNTHETIC_SECRET", domain=".douyu.com", secure=True)
            jar.set("other", "OTHER_SECRET", domain=".example.com")
            jar.set("expired", "EXPIRED_SECRET", domain=".douyu.com", expires=1)
            vault.save("douyu", jar)
            blob = (Path(directory) / "credentials" / "douyu.dpapi").read_bytes()
            self.assertNotIn(b"SYNTHETIC_SECRET", blob)
            restored = vault.load("douyu")
            self.assertEqual([(c.name, c.value) for c in restored], [("login_test", "SYNTHETIC_SECRET")])
            self.assertTrue(next(iter(restored)).secure)
            vault.delete("douyu")
            self.assertIsNone(vault.load("douyu"))

    def test_corrupted_encrypted_login_fails_without_echoing_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = CredentialStore(Path(directory))
            jar = requests.cookies.RequestsCookieJar()
            jar.set("login_test", "SYNTHETIC_SECRET", domain=".douyu.com")
            vault.save("douyu", jar)
            path = Path(directory) / "credentials" / "douyu.dpapi"
            path.write_bytes(b"SYNTHETIC_SECRET not encrypted")
            with self.assertRaises(ViewerError) as caught:
                vault.load("douyu")
            self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
