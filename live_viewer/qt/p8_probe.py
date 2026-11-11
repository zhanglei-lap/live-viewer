"""Isolated final-application PGC probe, anonymous or user-confirmed QR login."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
from PySide6.QtCore import QEventLoop, QTimer

from ..bangumi_validation import SAMPLES, expired_url
from ..core import new_session
from ..potplayer import PotPlayerAdapter
from ..settings import StateStore, atomic_write
from ..vod_state import progress_key


def probe(app, window, args):
    report = {"success": False, "stage": "P8-B", "frozen": bool(getattr(sys, "frozen", False)),
              "member": args.probe_p8_member, "full_requested": args.probe_p8_full, "checks": {}, "samples": []}
    page, runtime = window.bangumi_page, window.runtime
    output = args.probe_p8
    old_players = []
    def save():atomic_write(output, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    def wait(ms):
        loop = QEventLoop();QTimer.singleShot(ms, loop.quit);loop.exec()
    def pump(fn, timeout=60):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if window.closing:raise RuntimeError("user_cancelled_validation")
            wait(50)
            if fn():return True
        return False
    def check(name, value):
        report["checks"][name] = bool(value);save()
        if not value:raise RuntimeError(name)
    def playing():
        return runtime.controller.state == "playing" and runtime.player is not None and runtime.player.snapshot().get("owned_source") and runtime.player.snapshot().get("width", 0) > 0
    try:
        window.go_page(6)
        window.player_path.setText(str(args.probe_player));window.update_options()
        if args.probe_p8_member:
            # Fresh QR only; independent settings default to no login memory.
            report["status"] = "等待用户在手机确认 B 站扫码登录";save()
            page.mode.setCurrentIndex(1);window.qr_login("bilibili")
            check("user_confirmed_qr_login", pump(lambda: "bilibili" in window.cookies, float("inf")))
            window.tick()
        else:
            runtime.player_factory = lambda *a: PotPlayerAdapter(*a, hidden=True)
            window.config.update(volume=0, mute=True)
        for title, sid, ids in SAMPLES:
            for ep_id in ids:
                window.stop()
                page.input.setText(f"ep{ep_id}");page.lookup()
                check(f"metadata_{ep_id}", pump(lambda: page.season is not None and "pgc.lookup" not in runtime.jobs.pending))
                check(f"tracks_finished_{ep_id}", pump(lambda: "pgc.tracks" not in runtime.jobs.pending))
                check(f"identity_{ep_id}", page.season.season_id == sid and page.episode.ep_id == ep_id)
                if page.source is None:
                    notice = window.activity.notice_for("pgc.tracks")
                    check(f"expected_preview_{ep_id}", not args.probe_p8_member and notice is not None and notice.code == "pgc.preview_only")
                    report["samples"].append({"ep_id": ep_id, "status": "preview_only", "player_not_started": runtime.active is None});save();continue
                source = page.source
                if args.probe_p8_member:check(f"real_vip_{ep_id}", source.account["vip"] == "active")
                page.name.setText(title + " · 验收收藏");page.save();page.name.setText(title + " · 验收改名");page.save()
                check(f"favorite_index_{sid}", len([r for r in window.config["bangumi_favorites"] if r["season_id"] == sid]) == 1)
                page.load_subtitles();check(f"subtitle_catalog_{ep_id}", pump(lambda: "pgc.subtitles" not in runtime.jobs.pending))
                row = {"ep_id": ep_id, "media": source.summary(), "subtitle_status": page.subtitle_status.text()}
                report["samples"].append(row);save()
                page.resume_box.setChecked(False);page.play()
                check(f"native_playing_{ep_id}", pump(playing))
                player = runtime.player;old_players.append(player)
                native = player.snapshot()
                check(f"native_dimensions_{ep_id}", (native["width"], native["height"]) == (source.selected_video.width, source.selected_video.height))
                check(f"native_duration_{ep_id}", abs(native["duration_seconds"] - source.media_duration) < 3)
                row["native"] = {k: native.get(k) for k in ("width", "height", "fps", "duration_seconds", "owned_source")}
                target_seconds = source.media_duration + 40 if args.probe_p8_full and ep_id == 693247 else 20
                started = time.monotonic();last_saved = started;progress = []
                while time.monotonic() - started < target_seconds:
                    wait(100)
                    if window.closing:raise RuntimeError("user_cancelled_validation")
                    if runtime.controller.state == "ended":
                        check("full_episode_ended", args.probe_p8_full and ep_id == 693247)
                        break
                    check_state = runtime.controller.state
                    if check_state not in ("playing", "connecting"):raise RuntimeError(f"unexpected_playback_state_{check_state}")
                    if time.monotonic() - last_saved >= 10:
                        s = player.snapshot();progress.append({"elapsed": round(time.monotonic()-started, 1), "position": s.get("position_seconds"), "state": s.get("state")})
                        report["status"] = f"EP{ep_id} 持续播放 {time.monotonic()-started:.0f} 秒"
                        row["progress"] = progress;save();last_saved = time.monotonic()
                row["continuous_seconds"] = round(time.monotonic()-started, 1)
                if args.probe_p8_full and ep_id == 693247:
                    check("full_duration_watched", row["continuous_seconds"] >= source.media_duration - 5)
                    check("completed_bookmark", window.config["video_progress"][progress_key(runtime.active)]["completed"])
                else:
                    player.seek(30);check(f"seek_{ep_id}", pump(lambda: abs(player.snapshot().get("position_seconds", 0)-30) < 2, 15))
                    window.pause();check(f"pause_{ep_id}", pump(lambda: player.snapshot().get("state") == "paused", 8));wait(800)
                    position = player.snapshot()["position_seconds"]
                    window.runtime.reconnect();check(f"cold_recovery_{ep_id}", pump(lambda: runtime.player is not player and runtime.controller.state == "paused" and runtime.restore_target is None and runtime.player.snapshot().get("width", 0) > 0))
                    restored = runtime.player.snapshot()
                    row["recovery"] = {"before_position": position, "after_position": restored.get("position_seconds"), "state": restored.get("state")};save()
                    check(f"pause_position_retained_{ep_id}", abs(restored.get("position_seconds", 0)-position) < 2 and restored["state"] == "paused")
                    old_players.append(runtime.player);window.pause();check(f"continue_{ep_id}", pump(playing))
                row["status"] = "passed";save()
        # Exercise persistent resume and renewal on the first EP after all switches.
        window.stop();page.input.setText("ep693247");page.lookup();check("resume_catalog", pump(lambda: page.source is not None))
        page.resume_box.setChecked(False);page.play();check("resume_setup_playing", pump(playing))
        player = runtime.player;old_players.append(player)
        player.seek(47);check("checkpoint_seek", pump(lambda: abs(player.snapshot().get("position_seconds", 0)-47)<2, 15))
        window.pause();check("checkpoint_pause", pump(lambda: player.snapshot().get("state") == "paused", 8));wait(800)
        position = player.snapshot()["position_seconds"];relay = runtime.relay;serial = runtime.serial
        relay.tracks["vod-video.mp4"] = replace(relay.tracks["vod-video.mp4"], urls=tuple(expired_url(u) for u in relay.tracks["vod-video.mp4"].urls))
        response = {}
        def ranged():
            with new_session() as s:
                start = relay.sizes["vod-video.mp4"]//2
                with s.get(relay.url, headers={"Range": f"bytes={start}-{start+1023}"}, timeout=(5,60)) as r:
                    return {"status": r.status_code, "size": len(r.content)}
        runtime.jobs.submit("p8.range", ranged, lambda r: response.update(r), lambda e: response.update(error=e.code))
        check("warm_renewal_finished", pump(lambda: bool(response), 80))
        check("warm_renewal", response.get("status") == 206 and response.get("size") == 1024 and relay.refreshes == 1)
        check("warm_pause_position", runtime.serial == serial and player.snapshot()["state"] == "paused" and abs(player.snapshot()["position_seconds"]-position)<.5)
        window.stop();wait(300)
        stored = StateStore(window.store.directory).load()
        key = "pgc:693247:875212290"
        check("stored_checkpoint", abs(stored["video_progress"][key]["position"]-position)<.5)
        check("history_uses_ep_cid", any(r["ep_id"]==693247 and r["cid"]==875212290 for r in stored["bangumi_history"]))
        page.resume_box.setChecked(True);page.play();check("restart_resume", pump(lambda: playing() and runtime.restore_target is None and abs(runtime.player.snapshot().get("position_seconds", 0)-position)<8))
        old_players.append(runtime.player)
        check("no_ugc_history_pollution", not window.config["video_history"])
        report["picture_and_audio_confirmed_by_user"] = window.confirmed.isChecked()
        report["diagnostic"] = window.diagnostic_report()
        window.stop();runtime.close()
        check("owned_players_and_caches_released", all((p.process is None or p.process.poll() is not None) and (p.cache is None or not p.cache.path.exists()) for p in old_players))
        report["status"] = "验收完成";report["success"] = True
    except Exception as exc:
        report["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        save();window.close()
