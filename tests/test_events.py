import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from live_viewer.events import Event, EventCenter, JsonlStore


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.events = EventCenter(self.directory)

    def tearDown(self):
        self.events.close()
        self.temp.cleanup()

    def test_record_matches_approved_schema_and_is_immutable(self):
        schema = json.loads((Path(__file__).resolve().parent.parent / "docs/p0/event.schema.json").read_text(encoding="utf-8"))
        event = Event("playback.start_requested", "开始播放", details={"attempt": 1})
        self.assertEqual(set(event.record()), set(schema["required"]))
        self.assertRegex(event.code, schema["properties"]["code"]["pattern"])
        self.assertFalse(event.demo)
        with self.assertRaises(TypeError):
            event.details["attempt"] = 9
        with self.assertRaises(ValueError):
            Event("bad-code", "invalid")

    def test_headers_signed_urls_qr_keys_and_nested_objects_are_not_persisted_or_exported(self):
        event = self.events.emit(
            "diagnostic.failed",
            "地址 https://user:PROXY_SECRET@cdn.invalid/live.flv?auth=URL_SECRET\nCookie: login_test=COOKIE_SECRET\n"
            "qrcode_key=QR_SECRET",
            details={"host": "cdn.invalid", "stage": "CDN", "headers": {"Cookie": "NESTED_SECRET"},
                     "url": "https://cdn.invalid/NESTED_URL", "reason": "SESSDATA=DETAIL_SECRET"})
        output = self.directory / "export.json"
        self.events.store.export(output)
        combined = self.events.store.path.read_text(encoding="utf-8") + output.read_text(encoding="utf-8")
        for secret in ("PROXY_SECRET", "URL_SECRET", "COOKIE_SECRET", "QR_SECRET", "NESTED_SECRET", "DETAIL_SECRET"):
            self.assertNotIn(secret, combined)
        self.assertEqual(event.details["host"], "cdn.invalid")
        self.assertNotIn("headers", event.details)

    def test_concurrent_producers_have_complete_unique_disk_and_gui_records(self):
        def produce(index):
            for count in range(40):
                self.events.emit("worker.finished", f"{index}:{count}")
        threads = [threading.Thread(target=produce, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        rows = self.events.store.read()
        inbox, _ = self.events.drain()
        self.assertEqual(len(rows), 160)
        self.assertEqual([r["event_id"] for r in rows], [r.event_id for r in inbox])
        self.assertEqual(len({r["event_id"] for r in rows}), 160)

    def test_rotation_is_bounded_and_history_survives_restart(self):
        self.events.close()
        store = JsonlStore(self.directory, max_bytes=1400, backups=2)
        for count in range(40):
            self.assertTrue(store.append(Event("worker.finished", f"记录 {count}")))
        files = list(self.directory.glob(store.stem + ".jsonl*"))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size <= 1400 for path in files))
        store.close()
        import os
        for path in files:
            os.utime(path, (1700000000, 1700000000))  # Rotation order must not depend on filesystem timestamp resolution.
        restarted = JsonlStore(self.directory)
        rows = restarted.read()
        self.assertEqual(rows[-1]["message"], "记录 39")
        self.assertLess(len(rows), 40)
        restarted.close()

    def test_old_runs_are_pruned_without_overwriting_current_family(self):
        for count in range(8):
            store = JsonlStore(self.directory, keep_runs=3)
            self.assertTrue(store.append(Event("app.started", f"run {count}")))
            store.close()
        self.assertEqual(len(list(self.directory.glob("events-*.jsonl"))), 3)

    def test_corrupt_lines_are_skipped_and_torn_tail_is_separated_on_reopen(self):
        self.events.emit("app.started", "第一条")
        path = self.events.store.path
        self.events.store.file.close()
        self.events.store.file = None
        with path.open("ab") as output:
            output.write(b'{"message":"broken')
        self.events.emit("app.ready", "第二条")
        self.assertEqual([r["message"] for r in self.events.store.read()], ["第一条", "第二条"])

    def test_disk_failure_is_nonfatal_reported_once_and_cleared_on_recovery(self):
        with patch.object(self.events.store, "append", return_value=False):
            for _ in range(3):
                self.events.emit("room.selected", "选择房间")
        rows, _ = self.events.drain()
        self.assertEqual(sum(e.code == "log.write_failed" for e in rows), 1)
        self.assertEqual(self.events.notice().code, "log.write_failed")
        self.events.emit("room.selected", "再次选择")
        self.assertIsNone(self.events.notice())
        self.assertEqual(self.events.store.read()[-1]["code"], "log.write_recovered")

    def test_two_identical_failures_before_gui_poll_still_generate_one_current_reminder(self):
        for message in ("第一次失败", "第二次失败"):
            self.events.emit("relay.interrupted", message, level="ERROR", impact="interrupted",
                             presentation="strong", scope="playback", dedupe_key="playback.failure")
        rows, popups = self.events.drain()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(popups), 1)
        self.assertEqual(popups[0], self.events.notice())
        self.events.emit("relay.interrupted", "再次失败", level="ERROR", impact="interrupted",
                         presentation="strong", scope="playback", dedupe_key="playback.failure")
        self.assertEqual(self.events.drain()[1], [])

    def test_export_includes_unsaved_memory_events_when_disk_is_unavailable(self):
        with patch.object(self.events.store, "append", return_value=False):
            self.events.emit("room.selected", "磁盘写入失败时的活动")
        self.events.drain()
        output = self.directory / "memory-export.json"
        self.events.export(output)
        rows = json.loads(output.read_text(encoding="utf-8"))["events"]
        self.assertEqual({row["code"] for row in rows}, {"room.selected", "log.write_failed"})


if __name__ == "__main__":
    unittest.main()
