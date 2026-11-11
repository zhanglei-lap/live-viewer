"""Offline checks for the packaged executable; never access user credentials."""
from __future__ import annotations

import json
import base64
import sys
import time
import tempfile
import tkinter as tk
from pathlib import Path

import requests

from .app import VERSION, ViewerApp
from .core import PlatformClient, Quality, ViewerError, new_session, read_flv_sample
from .mpv_player import MpvPlayer, library_path
from .credentials import CredentialStore
from .relay import LocalRelay
from .qr_login import QrLogin, qr_png
from .settings import StateStore, remember_play


def live_sample(platform, preferred):
    client = PlatformClient(platform)
    try:
        try:
            return client, client.resolve(preferred), preferred
        except ViewerError:
            if platform != "bilibili":
                raise
        index, _ = client._api("https://api.live.bilibili.com/xlive/web-interface/v1/index/getList", params={"platform": "web"})
        rooms = []
        def visit(value):
            if isinstance(value, dict):
                number = value.get("roomid") or value.get("room_id")
                if number and str(number).isdigit():
                    rooms.append(str(number))
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)
        visit(index.get("data", {}))
        for room in list(dict.fromkeys(rooms))[:5]:
            try:
                return client, client.resolve(room), room
            except ViewerError:
                continue
        raise ViewerError("公开样本当前不可播放，请稍后重试自检。")
    except Exception:
        client.close()
        raise


def native_check(directory):
    """Real muted playback plus controlled relay failure; never disable host networking."""
    config = StateStore(directory).load()
    config.update(volume=0, mute=True, playback_backend="mpv")
    StateStore(directory).save(config)
    root = tk.Tk();root.withdraw()
    app = ViewerApp(root, directory)
    app.platform.set("斗鱼");app.room.set("6657");app.mode.set("匿名")
    app.play()
    stage, original_serial = 0, None
    result = {"success": False, "simulated_relay_failure": True}
    deadline = time.monotonic() + 480
    try:
        while time.monotonic() < deadline:
            root.update()
            window = app.embedded_window
            snapshot = window.player.snapshot() if window else {}
            if stage == 0 and snapshot.get("video_output_ready") and (snapshot.get("position_seconds") or 0) > 2:
                result["initial"] = snapshot
                original_serial = snapshot["load_serial"]
                app.relay.stop();stage = 1
            elif stage == 1 and snapshot.get("load_serial", 0) > original_serial and snapshot.get("video_output_ready") and (snapshot.get("position_seconds") or 0) > 1:
                result["recovered"] = snapshot
                result["retry_attempts"] = app.retry.total_attempts
                app.stop_playback();stage = 2;stopped_at = time.monotonic();original_serial = snapshot["load_serial"]
            elif stage == 2 and time.monotonic() - stopped_at > 1:
                result["stop_cancelled_retry"] = not app.retry.active and app.retry.due is None and window.player.load_serial == original_serial
                result["success"] = bool(result["stop_cancelled_retry"] and result["recovered"].get("playlist_count") == 1)
                break
            time.sleep(0.03)
        result["stage"] = stage
        return result
    finally:
        app.close()


def packaged_check(output: Path, *, live: bool = False, qr: bool = False, mpv: bool = False):
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {"version": VERSION, "frozen": bool(getattr(sys, "frozen", False)), "success": False}
    try:
        with tempfile.TemporaryDirectory(prefix="live-viewer-check-", dir=output.parent) as temporary:
            directory = Path(temporary).resolve()
            assert directory.parent == output.parent
            root = tk.Tk()
            root.withdraw()
            app = ViewerApp(root, directory)
            try:
                root.update()
                result["tk_pages"] = len(app.notebook.tabs()) == 3
                photo = tk.PhotoImage(master=root, data=base64.b64encode(qr_png("https://example.invalid/demo")), format="png")
                result["qr_render"] = photo.width() > 100
                video = tk.Frame(root, background="black")
                video.pack();root.update_idletasks()
                try:
                    library_path()
                except ViewerError:
                    result["mpv_optional_not_installed"] = True
                    result["mpv_initialized"] = True  # Optional in the PotPlayer edition.
                else:
                    player = MpvPlayer(video.winfo_id(), volume=0, mute=True)
                    result["mpv_initialized"] = bool(player.handle)
                    player.close()
                app.favorites.append({"platform": "douyu", "room": "6658", "name": "测试收藏"})
                app.history = remember_play(app.history, app.favorites[-1])
                app.platform.set("斗鱼")
                app.room.set("6658")
                app.room_name.set("自定义直播间")
                app.save_favorite()
                app.qualities = [Quality(4, "蓝光4M")]
                app.quality_box.configure(values=["原画优先", "蓝光4M"])
                app.quality.set("蓝光4M")
                app._write_config()
                from .p1_diagnostics import check_p1
                result["p1"] = check_p1(app)
                from .p2_diagnostics import check_p2
                result["p2"] = check_p2(directory)
            finally:
                app.close()
            saved = StateStore(directory).load()
            result["settings_persisted"] = saved["last_room"]["room"] == "6658" and saved["history"][0]["room"] == "6658"
            root = tk.Tk()
            root.withdraw()
            app = ViewerApp(root, directory)
            try:
                result["room_quality_restored"] = app._selection() == ("douyu", "6658", 4)
                result["room_name_restored"] = app.room_name.get() == "自定义直播间"
            finally:
                app.close()
            jar = requests.cookies.RequestsCookieJar()
            jar.set("test_login", "SYNTHETIC_TEST_SECRET", domain=".douyu.com", secure=True)
            vault = CredentialStore(directory)
            vault.save("douyu", jar)
            blob = (directory / "credentials" / "douyu.dpapi").read_bytes()
            result["login_encrypted"] = b"SYNTHETIC_TEST_SECRET" not in blob
            result["login_roundtrip"] = vault.load("douyu").get("test_login") == "SYNTHETIC_TEST_SECRET"
            vault.delete("douyu")
            result["login_deleted"] = vault.load("douyu") is None
            result["success"] = all(result[k] for k in ("tk_pages", "qr_render", "mpv_initialized", "settings_persisted", "room_quality_restored", "room_name_restored", "login_encrypted", "login_roundtrip", "login_deleted")) and result["p1"]["success"] and result["p2"]["success"]
            if mpv:
                result["native_playback"] = native_check(directory)
                result["success"] = result["success"] and result["native_playback"]["success"]
        if qr:
            result["qr"] = {}
            for platform in ("bilibili", "douyu"):
                flow = QrLogin(platform)
                try:
                    challenge = flow.create()
                    state = flow.poll()
                    entry = {"png_valid": challenge.png.startswith(b"\x89PNG\r\n\x1a\n"),
                             "expires_seconds": challenge.expires_seconds, "state": state.state}
                    result["qr"][platform] = entry
                    result["success"] = result["success"] and entry["png_valid"] and state.state == "waiting"
                finally:
                    flow.cancel()
                    flow.close()
        if live:
            result["live"] = {}
            for platform, room in (("bilibili", "35"), ("douyu", "6657")):
                client, stream, sample_room = live_sample(platform, room)
                try:
                    entry = {"preferred_sample": room, "sample_input_room": sample_room, "stream": stream.summary(),
                             "sample": read_flv_sample(client, stream, seconds=2)}
                finally:
                    client.close()
                relay = LocalRelay(platform, sample_room, None)
                relay.start()
                local_session = new_session()
                try:
                    with local_session.get(relay.url, stream=True, timeout=(8, 120)) as response:
                        chunks = response.iter_content(8192)
                        head = next(chunks, b"")
                        checked = len(head)
                        started = time.monotonic()
                        for chunk in chunks:
                            checked += len(chunk)
                            timing = relay.snapshot()["timing"]
                            if checked >= 2 * 1024 * 1024 or (time.monotonic() - started >= 2 and timing["forwarded_media_seconds"] is not None):
                                break
                        entry["relay_timing"] = relay.snapshot()["timing"]
                        entry["relay"] = {"status": response.status_code, "flv_header": head[:3] == b"FLV"}
                    result["live"][platform] = entry
                    result["success"] = result["success"] and entry["relay"]["status"] == 200 and entry["relay"]["flv_header"]
                    result["success"] = result["success"] and entry["relay_timing"]["forwarded_media_seconds"] is not None
                finally:
                    local_session.close()
                    relay.stop()
    except Exception as exc:
        # Exception strings can contain local paths or inputs; record only type.
        result["success"] = False
        result["error_type"] = type(exc).__name__
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result["success"]:
        raise SystemExit(1)
