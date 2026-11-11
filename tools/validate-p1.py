"""Repeatable P1 checks. All data goes into explicit temporary test directories."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def live_check(output_directory):
    from live_viewer.core import new_session
    from live_viewer.diagnostics import live_sample
    from live_viewer.events import EventCenter
    from live_viewer.playback import PlaybackController, PlaybackService
    results = {}
    with tempfile.TemporaryDirectory(prefix="p1-live-", dir=output_directory) as temporary:
        for platform, preferred in (("douyu", "6657"), ("bilibili", "1883358196")):
            client, _, actual_input = live_sample(platform, preferred)
            client.close()
            events = EventCenter(Path(temporary) / platform / "logs")
            controller = PlaybackController(events)
            service = PlaybackService(controller)
            request = {"platform": platform, "room_input": actual_input, "requested": None,
                       "room_name": "匿名短时检查", "session": "匿名", "backend": "potplayer",
                       "network_mode": "direct", "low_latency": True}
            token = controller.begin(request)
            relay = None
            session = new_session()
            try:
                relay, stream = service.prepare(request, None, token)
                started = time.monotonic()
                with session.get(relay.url, stream=True, timeout=(8, 25)) as response:
                    chunks = response.iter_content(8192)
                    head = next(chunks, b"")
                    size = len(head)
                    for chunk in chunks:
                        size += len(chunk)
                        if size >= 512 * 1024 or time.monotonic() - started >= 2:
                            break
                    events.drain()
                    recorded = events.store.read()
                    codes = {r["code"] for r in recorded}
                    results[platform] = {"preferred_room": preferred, "sample_input_room": actual_input,
                                         "stream": stream.summary(), "http_status": response.status_code,
                                         "flv_header": head[:3] == b"FLV", "bytes_read": size,
                                         "events_received": sorted(codes), "player_launched": False,
                                         "decoded_verified": False,
                                         "success": response.status_code == 200 and head[:3] == b"FLV"
                                         and {"relay.source_ready", "relay.forwarding", "playback.resolved"} <= codes}
            finally:
                controller.stop()
                session.close()
                if relay:
                    relay.stop()
                events.close()
    return results


def screenshots(directory):
    import tkinter as tk
    import ctypes
    from PIL import ImageGrab
    from live_viewer.app import ViewerApp
    from live_viewer.core import ViewerError
    if os.name == "nt":
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="p1-gui-", dir=directory.parent) as temporary:
        root = tk.Tk()
        app = ViewerApp(root, Path(temporary))
        try:
            request = {"platform": "bilibili", "room_input": "35", "requested": None,
                       "room_name": "B站测试房间", "session": "匿名", "backend": "potplayer",
                       "network_mode": "direct", "low_latency": True}
            app.controller.begin(request)
            token = app.controller.token
            app.controller.fail(token, ViewerError("该 B 站房间当前未开播（测试告示）。", code="room.offline", level="WARN"))
            app._poll()
            root.update()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            ImageGrab.grab(window=hwnd).save(directory / "01-offline-notice.png")
            app.controller.begin(request)
            app.controller.fail(app.controller.token, ViewerError("CDN：TLS / 证书连接失败（测试域名；直连）。",
                                                                  code="network.tls_failed"))
            app._drain_activity()
            if app.alert_window:
                app.alert_window.withdraw()
            app._log("模拟登录成功；播放故障仍显示在固定告示中。", code="account.imported", target="bilibili")
            root.update()
            ImageGrab.grab(window=hwnd).save(directory / "02-failure-with-login-success.png")
        finally:
            app.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".research/p1-validation.json")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"stage": "P1", "success": True}
    if not args.skip_tests:
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=1).run(unittest.defaultTestLoader.discover(str(ROOT / "tests")))
        report["tests"] = {"run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                           "skipped": len(result.skipped), "success": result.wasSuccessful()}
        report["success"] = result.wasSuccessful()
        if not result.wasSuccessful():
            print(output.getvalue(), flush=True)
    if args.live:
        try:
            report["live"] = live_check(args.output.parent)
            report["success"] &= all(r["success"] for r in report["live"].values())
        except Exception as exc:
            report["success"] = False
            report["live_error_type"] = type(exc).__name__
    if args.screenshots:
        screenshots(args.output.parent / "p1-screenshots")
        report["screenshots"] = str(args.output.parent / "p1-screenshots")
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True), flush=True)
    raise SystemExit(0 if report["success"] else 1)


if __name__ == "__main__":
    main()
