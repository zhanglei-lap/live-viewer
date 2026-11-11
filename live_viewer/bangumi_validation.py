"""Independent P8-A evidence collection; no stable settings or credentials read."""
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .bangumi import BangumiClient, BangumiProxy
from .core import ViewerError, new_session
from .potplayer import PotPlayerAdapter


SAMPLES = (("孤独摇滚！", 43164, (693247, 706287)),
           ("葬送的芙莉莲", 46089, (779775, 809819)),
           ("间谍过家家", 41410, (508404, 516901)))
NATIVE_FIELDS = ("state", "position_seconds", "duration_seconds", "width", "height", "fps", "owned_source", "source_present")


def native_evidence(player):
    return {k: player.snapshot().get(k) for k in NATIVE_FIELDS}


def check_cancel(cancel):
    if cancel.is_set():
        raise ViewerError("验证已取消。", code="playback.cancelled", level="DEBUG")


def error_record(exc):
    return {"status": exc.code, "message": str(exc)} if isinstance(exc, ViewerError) else {
        "status": "validation.unexpected", "message": "验证发生内部错误，未输出可能含敏感数据的异常正文。"}


def network_evidence():
    with new_session(network_mode="direct") as session:
        merged = session.merge_environment_settings("https://api.bilibili.com/", {}, False, None, None)
        return {"mode": "direct", "trust_env": session.trust_env, "merged_proxy_count": len(merged["proxies"]),
                "environment_proxy_present": any(os.environ.get(k) for k in
                    ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")),
                "d3_mainland_without_vpn": "unverified", "system_route_or_vpn": "not_inferred"}


def new_report():
    return {"schema": "p8a-validation-v1", "created_utc": datetime.now(timezone.utc).isoformat(),
            "network": network_evidence(), "samples": [], "events": [],
            "human_checks": {"official_same_account_baseline": "unverified", "picture_audio_sync": "unverified"},
            "gate": "pending_real_member_and_native_evidence"}


def inspect_matrix(jar=None, *, cancel=None, on_row=lambda row: None):
    cancel = cancel or threading.Event()
    report = new_report()
    client = BangumiClient(jar)
    try:
        for name, sid, ids in SAMPLES:
            for ep_id in ids:
                check_cancel(cancel)
                try:
                    inspected = client.inspect(f"ep{ep_id}")
                    row = inspected.summary()
                    row["identity_matches_sample"] = (inspected.season.season_id == sid and inspected.season.title == name)
                    row["native"] = None
                except Exception as exc:
                    row = {"season_title": name, "episode": {"season_id": sid, "ep_id": ep_id}, **error_record(exc), "native": None}
                check_cancel(cancel)
                report["samples"].append(row)
                on_row(row)
    finally:
        client.close()
    return report


def expired_url(url):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    for key, value in (("deadline", "1"), ("upsig", "0"), ("wsTime", "00000001"), ("wsSecret", "0")):
        if key in query or key == "deadline":
            query[key] = value
    return urlunparse(parsed._replace(query=urlencode(query)))


def run_native(value, jar, directory, executable, *, video_key=None, requested=127, duration=300,
               full_episode=False, verify_eos=False, hidden=False, cancel=None, on_progress=lambda message: None):
    """One owned instance; canceled workers may never launch or replace a player."""
    cancel = cancel or threading.Event()
    result = {"status": "pending", "checks": {}, "continuous_seconds": 0, "samples": [],
              "full_episode_requested": bool(full_episode), "audio_picture_human": "unverified",
              "network": network_evidence()}
    player = relay = None
    event_lock = threading.Lock()
    def event(code, message, **fields):
        with event_lock:
            result.setdefault("events", []).append({"code": code, "message": message, "level": fields.get("level", "INFO"),
                                                    "host": (fields.get("details") or {}).get("host", "")})
    def resolve(old=None):
        client = BangumiClient(jar)
        try:
            return client.resolve(value, expected_cid=old.episode.cid if old else None,
                video_key=old.selected_video.key if old else video_key, audio_key=old.selected_audio.key if old else None,
                requested=old.requested if old else requested)
        finally:
            client.close()
    def check(name, passed):
        result["checks"][name] = bool(passed)
        if not passed:
            raise ViewerError("番剧实播验证未通过：" + name, code="validation.native_failed")
    def wait_for(predicate, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            check_cancel(cancel)
            if predicate():
                return True
            cancel.wait(.1)
        return False
    def playing():
        s = player.snapshot()
        return s.get("owned_source") and s.get("state") == "playing" and s.get("width", 0) > 0
    cache = profile = process = None
    try:
        check_cancel(cancel)
        source = resolve()
        result["media"] = source.summary()
        check_cancel(cancel)
        relay = BangumiProxy(source, on_event=event, refresher=resolve)
        relay.prepare()
        check_cancel(cancel)
        relay.start()
        with relay._session() as session:
            result["cdn_policy"] = {"trust_env": session.trust_env,
                "merged_proxy_count": len(session.merge_environment_settings(source.selected_video.urls[0], {}, False, None, None)["proxies"]),
                "hosts": sorted({urlparse(u).hostname for t in (source.selected_video, source.selected_audio) for u in t.urls})}
        player = PotPlayerAdapter(directory, executable, hidden=hidden)
        player.prepare(replay=False)
        check_cancel(cancel)
        # Sound is enabled only for the interactive user; automated probes are muted.
        player.open(relay.url, source.video.title, audio_url=relay.audio_url, volume=0 if hidden else 60,
                    mute=hidden, guard=lambda: not cancel.is_set())
        check("owned_native_playing", wait_for(playing, 50))
        native = player.snapshot()
        check("decoded_dimensions_match_track", (native.get("width"), native.get("height")) ==
              (source.selected_video.width, source.selected_video.height))
        check("native_full_duration", abs(native.get("duration_seconds", 0) - source.media_duration) <= 3)
        try:
            ratio = source.selected_video.fps.split("/")
            declared_fps = float(ratio[0]) / (float(ratio[1]) if len(ratio) == 2 else 1)
        except (ValueError, ZeroDivisionError):
            declared_fps = 0
        check("native_fps_matches_metadata", declared_fps > 0 and abs(native.get("fps", 0) - declared_fps) < .1)
        result["initial_native"] = native_evidence(player)
        process, cache, profile = player.process, player.cache.path if player.cache else None, player.profile.path
        start = last_move = last_sample = time.monotonic()
        last_position = native.get("position_seconds", 0)
        previous = last_position
        limit = source.media_duration + 60 if full_episode else duration
        on_progress(source.video.title + "：开始连续实播。")
        while time.monotonic() - start < limit:
            check_cancel(cancel)
            s = player.snapshot()
            now = time.monotonic()
            position = s.get("position_seconds", 0)
            if position > previous + .05:
                last_move = now
                previous = position
            if s.get("state") == "ended":
                if full_episode and previous >= source.media_duration - 3:
                    result["checks"]["natural_episode_end"] = True
                    break
                raise ViewerError("实播结束早于目标连续时长。", code="validation.early_end")
            if s.get("state") in ("closed", "fault", "source_changed") or not s.get("owned_source"):
                raise ViewerError("播放器已关闭或播放源归属改变，验证已停止。", code="validation.player_closed")
            if now - last_move > 20:
                raise ViewerError("连续实播进度超过 20 秒未前进。", code="validation.stalled")
            if now - last_sample >= 10:
                result["samples"].append({"elapsed": round(now - start, 1), **native_evidence(player)})
                last_sample = now
                on_progress(f"{source.season_title} EP{source.episode.ep_id}：已实播 {now-start:.0f} 秒。")
            cancel.wait(.2)
        result["continuous_seconds"] = round(time.monotonic() - start, 1)
        result["end_native"] = native_evidence(player)
        check("continuous_play", result["continuous_seconds"] >= (source.media_duration - 5 if full_episode else duration - 1))
        check("native_progress_advanced", previous > last_position + min(10, duration * .5))
        if full_episode:
            check("natural_episode_end", result["checks"].get("natural_episode_end"))
            result["full_watch_passed"] = True
        if verify_eos and not full_episode:
            player.seek(max(0, source.media_duration - 6))
            check("seeked_eos_reached", wait_for(lambda: player.snapshot().get("state") == "ended", 20))
        if full_episode or verify_eos:
            # Dual-track open is intentionally a fresh owned instance, as in P7.
            # EOF observation remains separate from post-EOF control checks.
            ended_process, ended_cache, ended_profile = player.process, player.cache.path if player.cache else None, player.profile.path
            player.close(wait=True)
            check("ended_instance_released", (ended_process is None or ended_process.poll() is not None) and
                  (ended_cache is None or not ended_cache.exists()) and not ended_profile.exists() and not player.worker.is_alive())
            check_cancel(cancel)
            player = PotPlayerAdapter(directory, executable, hidden=hidden)
            player.prepare(replay=False)
            check_cancel(cancel)
            player.open(relay.url, source.video.title, audio_url=relay.audio_url, mute=hidden, volume=0 if hidden else 60,
                        guard=lambda: not cancel.is_set())
            check("reopen_after_end", wait_for(playing, 40))
            process, cache, profile = player.process, player.cache.path if player.cache else None, player.profile.path
        player.seek(60)
        check("seek_forward", wait_for(lambda: abs(player.snapshot().get("position_seconds", 0) - 60) < 1.5))
        player.seek(20)
        check("seek_backward", wait_for(lambda: abs(player.snapshot().get("position_seconds", 0) - 20) < 1.5))
        player.pause()
        check("pause", wait_for(lambda: player.snapshot().get("state") == "paused"))
        check_cancel(cancel)
        cancel.wait(1)
        before = player.snapshot().get("position_seconds", 0)
        old_serial = player.serial
        # Invalidate signed addresses, then issue a real Range request. A successful
        # renewal must come from PGC and keep EP/cid, keys, prefix hash and length.
        relay.tracks["vod-video.mp4"] = replace(relay.tracks["vod-video.mp4"],
            urls=tuple(expired_url(u) for u in relay.tracks["vod-video.mp4"].urls))
        with new_session() as session:
            middle = relay.sizes["vod-video.mp4"] // 2
            with session.get(relay.url, headers={"Range": f"bytes={middle}-{middle+1023}"}, timeout=(5, 60)) as response:
                check("expired_url_range_recovered", response.status_code == 206 and len(response.content) == 1024 and relay.refreshes == 1)
        check("refresh_preserves_pause_position", player.snapshot().get("state") == "paused" and
              abs(player.snapshot().get("position_seconds", 0) - before) < .5 and player.serial == old_serial)
        fresh = relay.source_info
        check("renew_ep_cid_tracks_pinned", (fresh.episode.ep_id, fresh.episode.cid, fresh.episode.season_id,
              fresh.selected_video.key, fresh.selected_audio.key) == (source.episode.ep_id, source.episode.cid,
              source.episode.season_id, source.selected_video.key, source.selected_audio.key))
        player.resume()
        check("resume", wait_for(playing))
        result["status"] = "passed"
    except Exception as exc:
        result.update(error_record(exc))
    finally:
        if player:
            process = player.process or process
            cache = player.cache.path if player.cache else cache
            profile = player.profile.path or profile
            player.close(wait=True)
        if relay:
            relay.stop()
        result["cleanup"] = {"player_exited": process is None or process.poll() is not None,
            "cache_removed": cache is None or not Path(cache).exists(), "run_profile_removed": profile is None or not Path(profile).exists(),
            "listener_stopped": relay is None or relay.server is None or
                                relay.server.socket.fileno() == -1 and not relay.thread.is_alive(),
            "worker_stopped": player is None or not player.worker.is_alive()}
        if not all(result["cleanup"].values()) and result["status"] == "passed":
            result.update(status="validation.cleanup_failed", message="播放器或本机会话未完成清理。")
    return result


def evaluate_gate(report):
    rows = report.get("samples", [])
    reasons = []
    member_rows = [r for r in rows if r.get("account", {}).get("login") == "authenticated"]
    def runs(row):
        return [r for r in [row.get("native"), *row.get("native_runs", [])] if isinstance(r, dict)]
    for _, sid, ids in SAMPLES:
        for ep_id in ids:
            matches = [r for r in member_rows if r.get("episode", {}).get("ep_id") == ep_id]
            if not matches or not any(r.get("identity_matches_sample") and r.get("status") == "pgc.whole" and
                                      any(n.get("status") == "passed" and n.get("continuous_seconds", 0) >= 299 for n in runs(r))
                                      for r in matches):
                reasons.append(f"EP{ep_id} 的会员整集五分钟实播尚未通过")
    hd = [n for r in member_rows if r.get("account", {}).get("vip") == "active" for n in runs(r)
          if n.get("status") == "passed" and (n.get("media") or {}).get("actual", 0) in (112, 116, 120)]
    if not hd:
        reasons.append("真实会员高清档位尚未取得并完成实播")
    # A completed whole watch can remain valid when a later control check fails.
    # Every episode must still have a separate successful control/cleanup run above.
    def full_watch(n):
        checks = n.get("checks") or {}
        media = n.get("media") or {}
        return (n.get("full_episode_requested") and checks.get("natural_episode_end") and checks.get("continuous_play")
                and checks.get("decoded_dimensions_match_track") and checks.get("native_full_duration")
                and n.get("continuous_seconds", 0) >= media.get("media_duration_seconds", float("inf")) - 5)
    if not any(full_watch(n) for r in member_rows for n in runs(r)):
        reasons.append("一集完整观看尚未完成")
    if not report.get("d2_actual_api_and_cdn_passed"):
        reasons.append("存在代理配置时的实际 API / CDN 隔离尚未通过")
    if report.get("human_checks", {}).get("official_same_account_baseline") != "confirmed" or report.get("human_checks", {}).get("picture_audio_sync") != "confirmed":
        reasons.append("官网同账号基准和人工音画确认尚未完成")
    report["gate_reasons"] = reasons
    report["gate"] = ("passed_d3_user_confirmed" if report.get("network", {}).get("d3_mainland_without_vpn") == "user_confirmed"
                      else "passed_d3_unverified") if not reasons else "pending"
    return report
