"""GUI-independent orchestration for the Qt frontend, using the P1/P2 services."""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from .core import ViewerError
from .playback import OperationController, PlaybackController, PlaybackService, safe_error
from .potplayer import PotPlayerAdapter
from .relay import RelayEndpoint
from .rtmp import RtmpEndpoint
from .settings import remember_play, remember_video
from .resources import available_memory
from .vod_state import bookmark, progress_key
from .resources import available_memory


class Jobs:
    """Workers never touch widgets. Cancelled resource results are disposed off-thread."""
    def __init__(self, events):
        self.events = events
        self.operations = OperationController()
        self.queue = queue.Queue()
        self.lock = threading.RLock()
        self.pending = {}
        self.closed = False

    def submit(self, channel, fn, done, failed, *, target=None, valid=lambda: True, dispose=None):
        token = self.operations.begin(channel, target)
        with self.lock:
            self.pending[channel] = token

        def run():
            result, error = None, None
            try:
                result = fn()
            except Exception as exc:
                error = safe_error(exc)
            with self.lock:
                accepted = not self.closed and self.operations.current(token) and valid()
                if accepted:
                    self.queue.put((token, result, error, done, failed, valid, dispose))
            if not accepted and result is not None and dispose is not None:
                dispose(result)

        threading.Thread(target=run, name=channel, daemon=True).start()
        return token

    def cancel(self, channel):
        with self.lock:
            self.operations.cancel(channel)
            self.pending.pop(channel, None)

    def drain(self):
        while True:
            try:
                token, result, error, done, failed, valid, dispose = self.queue.get_nowait()
            except queue.Empty:
                return
            accepted = not self.closed and self.operations.current(token) and valid()
            if self.pending.get(token.channel) == token:
                self.pending.pop(token.channel, None)
            if not accepted:
                if result is not None and dispose:
                    threading.Thread(target=dispose, args=(result,), daemon=True).start()
                self.events.emit("operation.stale_result", "已丢弃取消或切换前的后台结果。",
                                 source="operation", level="DEBUG", target=token.target)
                continue
            try:
                failed(error) if error else done(result)
            except Exception as exc:
                failed(safe_error(exc))

    def close(self):
        with self.lock:
            self.closed = True
            self.operations.close()
            self.pending.clear()
        self.drain()


class LiveRuntime:
    """Called by a GUI timer on one thread; networking/control stay in workers."""
    def __init__(self, events, directory, config, cookies, *, persist=lambda: None,
                 player_factory=PotPlayerAdapter, service_factory=PlaybackService):
        self.events, self.directory, self.config, self.cookies = events, Path(directory), config, cookies
        self.persist = persist
        self.controller = PlaybackController(events)
        self.service = service_factory(self.controller)
        self.jobs = Jobs(events)
        self.player_factory = player_factory
        self.player = None
        self.endpoint = RelayEndpoint()
        self.replay_endpoint = RtmpEndpoint()
        self.player_options = None
        self.replacing_player = False
        self.relay = self.stream = None
        self.active = self.last_request = None
        self.token = self.player_token = None
        self.serial = None
        self.paused = self.closed = False
        self.resume_pending_at = None
        self.media_closed_at = None
        self.expected_absence = True
        self.expected_closed_media = self.had_graph = False
        self.expected_pause = False
        self.position = self.progress_at = None
        self.history_recorded = True
        self.last_stats_at = 0.0
        self.retiring = []
        self.last_report = {}
        self.last_vod_data = None
        self.last_bookmark_at = 0.0
        self.restore_target = None
        self.restore_deadline = 0.0
        self.last_resource_check = 0.0
        self.last_resource_check = 0.0

    @property
    def busy(self):
        return "playback.prepare" in self.jobs.pending

    def error(self, exc, *, scope="playback.command"):
        exc = safe_error(exc)
        source = scope.partition(".")[0]
        blocked = source in ("playback", "player")
        self.events.emit(exc.code, str(exc), source=source, level=exc.level,
                         presentation="strong", impact="start_blocked" if blocked else "degraded", scope=scope)

    def _retire(self, relay):
        if relay is None:
            return
        relay.stop_event.set()
        self.endpoint.detach(relay)
        thread = threading.Thread(target=relay.stop, daemon=True)
        self.retiring = [t for t in self.retiring if t.is_alive()]
        self.retiring.append(thread)
        thread.start()

    def play(self, request, *, automatic=False):
        if self.closed or self.busy:
            return False
        if not automatic:self.save_video_progress()
        request = dict(request)
        vod = request.get("kind") == "vod"
        if vod:
            request["replay_mode"] = request["low_latency"] = False
            if automatic:
                request = self.video_recovery_request(request)
        request.setdefault("replay_mode", self.config.get("native_replay", True))
        request.setdefault("replay_minutes", self.config.get("replay_minutes", 300))
        request.setdefault("vsr", self.config.get("vsr", False))
        try:
            path = Path(self.config["player"].strip().strip('"')).resolve()
            if not path.is_file() or path.suffix.lower() != ".exe":
                raise ViewerError("请在设置页选择已安装的 64 位 PotPlayer。", code="player.path_invalid")
            jar = self.cookies.get(request["platform"]) if request["session"] == "导入 Cookie" else None
            if request["session"] == "导入 Cookie" and jar is None:
                raise ViewerError("当前平台没有可用登录，请选择匿名观看或导入登录。",
                                  code="account.missing", level="WARN")
            options = (request["replay_mode"], request["replay_minutes"], request["vsr"])
            if self.player is not None and (self.player.profile.source != path or
                                            self.player_options is not None and self.player_options != options or
                                            not automatic and (vod or (self.last_request or {}).get("kind") == "vod")):
                old = self.player
                thread = threading.Thread(target=lambda: old.close(wait=True), daemon=True)
                self.retiring.append(thread)
                thread.start()
                self.player = None
            if self.player is None:
                self.player = self.player_factory(self.directory, path)
            self.player_options = options
            player = self.player
            prior = player.snapshot()
            self.expected_absence = not automatic and (prior.get("state") in ("idle", "closed") or not player.ever_launched)
            self.expected_closed_media = not automatic and prior.get("state") == "closed_media"
            self.expected_pause = not automatic and prior.get("state") == "paused"
            if not automatic:
                self._retire(self.relay)
                self.relay = self.stream = None
            self.events.clear("playback.command")
            self.events.clear("player")
            token = self.token = self.controller.begin(request, automatic=automatic)
            self.active = self.last_request = request
            self.paused = self.controller.retry.paused = False
            if not automatic:
                self.controller.retry.start()
                self.controller.retry.active = self.config["auto_reconnect"]
        except Exception as exc:
            self.error(exc)
            return False

        def work():
            player.prepare(replay=request["replay_mode"], replay_minutes=request["replay_minutes"], vsr=request["vsr"])
            return self.service.prepare(request, jar, token, endpoint=self.endpoint,
                                        replay_endpoint=self.replay_endpoint if request["replay_mode"] else None, start=False,
                                        cookie_provider=lambda: self.cookies.get(request["platform"]) if request["session"] == "导入 Cookie" else None)

        def failed(exc):
            blocked = exc.code in ("room.offline", "account.expired", "account.missing") or exc.code.startswith("player.")
            recoverable_video = exc.code in ("vod.network_failed", "vod.cdn_failed", "vod.url_expired", "vod.read_incomplete", "pgc.network_failed")
            recovering = self.config["auto_reconnect"] and not blocked and (not vod or automatic and recoverable_video)
            if blocked:
                self.controller.retry.stop()
            if vod and not recovering:self.controller.retry.stop()
            self.controller.fail(token, exc, recovering=recovering)
            if recovering:
                self._schedule_retry()

        def attach(result):
            relay, stream = result
            if player.snapshot().get("state") == "closed" and not self.expected_absence:
                self._retire(relay)
                self.stop(stop_engine=False)
                return
            try:
                if self.relay is not None:
                    self._retire(self.relay)
                relay.start()
                self.relay, self.stream = relay, stream
                self.player_token = token
                self.position = self.progress_at = None
                self.had_graph = self.history_recorded = False
                extra = {"audio_url": relay.audio_url, "start_position": request.get("resume_position", 0),
                         "start_paused": request.get("resume_paused", False), "subtitle_text": stream.subtitle_text} if vod else {}
                self.restore_target = request.get("resume_position") if vod and request.get("resume_position", 0) > 0 else None
                self.restore_deadline = time.monotonic() + 20
                self.serial = player.open(relay.url, request["room_name"], volume=self.config["volume"],
                                          mute=self.config["mute"],
                                          guard=lambda: self.controller.current(token) and not self.paused,
                                          allow_new_process=self.expected_absence or not player.ever_launched, **extra)
                self.controller.publish(token, "player.open_requested",
                                        f"已发送至 PotPlayer；实际返回 {stream.actual_name}。", state="connecting")
            except Exception as exc:
                self._retire(relay)
                self.relay = self.stream = None
                failed(safe_error(exc))

        def done(result):
            if automatic and (request["replay_mode"] or vod) and self.player is not None:
                old = self.player
                self.replacing_player = True
                def replace():
                    fresh = None
                    try:
                        if old.snapshot().get("state") == "closed" or not self.controller.current(token):
                            raise ViewerError("播放器已关闭。", code="player.user_closed", level="INFO")
                        old.close(wait=True)
                        fresh = self.player_factory(self.directory, path)
                        fresh.serial = old.serial
                        fresh.prepare(replay=request["replay_mode"], replay_minutes=request["replay_minutes"], vsr=request["vsr"])
                        return fresh
                    except Exception:
                        if fresh is not None:
                            fresh.close(wait=True)
                        result[0].stop()
                        raise
                def replaced(fresh):
                    nonlocal player
                    player = self.player = fresh
                    self.replacing_player = False
                    self.expected_absence = True
                    self.expected_closed_media = False
                    self.had_graph = False
                    self.serial = None
                    attach(result)
                def replacement_failed(exc):
                    self.replacing_player = False
                    self._retire(result[0])
                    if exc.code == "player.user_closed":
                        self.stop(stop_engine=False)
                    else:
                        failed(exc)
                self.jobs.submit("playback.prepare", replace, replaced, replacement_failed, target=token.target,
                                 valid=lambda: self.controller.current(token), dispose=lambda fresh: (fresh.close(wait=True), result[0].stop()))
            else:
                attach(result)

        self.jobs.submit("playback.prepare", work, done, failed, target=token.target,
                         valid=lambda: self.controller.current(token), dispose=lambda r: r[0].stop())
        return True

    def _schedule_retry(self):
        retry = self.controller.retry
        if self.active and self.active.get("kind") == "vod" and retry.total_attempts >= 3:
            retry.stop()
            if self.controller.state == "reconnecting":self.controller.fail(self.token, ViewerError("视频恢复已达到三次上限，请检查网络或手动重试。", code="vod.recovery_exhausted"))
            return
        previous = retry.due
        retry.failure()
        if previous is None and retry.due is not None:
            self.controller.publish(self.token, "playback.retry_scheduled", f"已安排第 {retry.attempts} 次重连。",
                                    level="WARN", details={"attempt": retry.attempts})

    def reconnect(self):
        if (self.active or self.last_request or {}).get("kind") == "vod":
            return self.play(self.video_recovery_request(self.active or self.last_request))
        if self.active and self.active.get("replay_mode") and self.player is not None:
            snapshot = self.player.snapshot()
            if snapshot.get("owned_source") and snapshot.get("duration_seconds", 0) > 0 and snapshot.get("state") in ("playing", "paused"):
                self.player.seek(max(0, snapshot["duration_seconds"] - 2))
                self.paused = self.controller.retry.paused = False
                self.resume_pending_at = time.monotonic()
                self.player.resume()
                self.controller.publish(self.token, "playback.live_edge_requested", "已回到当前直播附近，回看缓存继续保留。",
                                        state="connecting")
                return True
        if self.active or self.last_request:
            return self.play(self.active or self.last_request)
        self.events.emit("playback.no_previous_room", "请先播放一个房间，再使用回到直播。", source="playback")
        return False

    def set_auto_reconnect(self, enabled):
        self.config["auto_reconnect"] = enabled
        retry = self.controller.retry
        if not enabled:
            retry.stop()
            if self.controller.state == "reconnecting" and not self.busy:
                self.controller.publish(self.token, "playback.auto_reconnect_disabled",
                    "自动恢复已关闭；直播中断时可手动回到直播。", state="error")
        elif self.active is not None:
            notice = self.events.notice_for("playback")
            recoverable = notice is not None and (notice.code.startswith(("relay.", "network.")) or
                          notice.code in ("player.interrupted", "operation.failed", "operation.unexpected"))
            if self.controller.state not in ("unavailable", "error") or recoverable:
                retry.start()
                retry.paused = self.paused
                if self.controller.state == "error" and not self.busy:
                    self._schedule_retry()

    def pause(self):
        if self.player is None or self.active is None:
            return
        if self.paused:
            fault = self.events.notice_for("playback")
            if self.active.get("kind") == "vod" and fault and fault.impact == "interrupted":
                request = self.video_recovery_request(self.active);request["resume_paused"] = False
                self.play(request)
                return
            if self.active.get("replay_mode") or self.active.get("kind") == "vod":
                self.paused = self.controller.retry.paused = False
                self.resume_pending_at = time.monotonic()
                self.player.resume()
                self.controller.publish(self.token, "playback.resumed", "继续从暂停位置播放。" if self.active.get("kind") == "vod" else "继续从暂停位置播放，可使用回到直播。",
                                        state="connecting")
            else:
                self.play(self.active)
        else:
            self.paused = True
            self.replacing_player = False
            self.jobs.cancel("playback.prepare")
            self.controller.pause()
            self.player.pause()

    def stop(self, *, stop_engine=True):
        self.save_video_progress()
        if self.active is not None:
            self.last_report = self.report()
        self.controller.stop(closing=self.closed)
        self.jobs.cancel("playback.prepare")
        self.token = self.active = None
        self.paused = False
        self.replacing_player = False
        self.restore_target = None
        if stop_engine and self.player is not None:
            self.player.stop()
        self._retire(self.relay)
        self.relay = self.stream = None

    def video_recovery_request(self, request):
        result = dict(request)
        self.save_video_progress()
        data = self.last_vod_data
        if data and data.get("key", f"{data.get('bvid')}:{data.get('cid')}") == progress_key(request):
            result["resume_position"] = 0 if data.get("completed") else data["position"]
        result["resume_paused"] = self.paused
        if self.relay is not None and hasattr(self.relay, "identities"):
            result["expected_media"] = {"sizes": dict(self.relay.sizes), "identities": dict(self.relay.identities)}
        return result

    def save_video_progress(self, data=None):
        if self.active is None or self.active.get("kind") != "vod":return
        fault = self.events.notice_for("playback")
        if data is not None and fault and fault.code == "vod.resume_failed":return
        if data is not None and self.restore_target is None and data.get("owned_source") and data.get("width", 0) > 0 and data.get("duration_seconds", 0) > 0:
            self.last_vod_data = {"bvid": self.active.get("bvid"), "cid": self.active["cid"], "key": progress_key(self.active),
                "position": data.get("position_seconds", 0), "duration": data["duration_seconds"],
                "completed": data["state"] == "ended" and (data.get("position_seconds") or 0) >= data["duration_seconds"] - 2}
        observed = self.last_vod_data
        if observed and observed.get("key", f"{observed.get('bvid')}:{observed.get('cid')}") == progress_key(self.active):
            if bookmark(self.config, self.active, observed["position"], observed["duration"], completed=observed["completed"]):
                if data is None or time.monotonic() - self.last_bookmark_at > 15:
                    self.last_bookmark_at = time.monotonic();self.persist()

    def report(self, *, confirmed=False, note=""):
        from .reports import clean, PLAYBACK
        if self.active is not None:
            result = {**self.active, **self.controller.snapshot()}
            if self.relay is not None:
                result["transport"] = self.relay.snapshot()
            if self.player is not None:
                result["potplayer"] = self.player.snapshot()
            result["decoded_verified"] = False
            result["auto_reconnect_attempts"] = self.controller.retry.total_attempts
        else:
            result = dict(self.last_report)
            if result:
                result["state"] = self.controller.state
        received = result.get("transport", {}).get("bytes_forwarded", 0)
        result["picture_and_audio_confirmed_by_user"] = bool(confirmed and received > 0)
        result["user_note"] = note
        return clean(result, PLAYBACK)

    def _poll_player(self):
        if self.player is None:
            return
        player, retry = self.player, self.controller.retry
        data = player.snapshot()
        for event in player.poll():
            if event["kind"] == "cleanup_deferred":
                self.events.emit("player.cleanup_deferred", "专用播放器临时文件清理已延后。", level="WARN", source="player")
            elif event.get("serial") == self.serial and self.controller.current(self.player_token):
                if event["kind"] == "error":
                    self.controller.fail(self.player_token, event["error"])
                    retry.stop()
                elif event["kind"] in ("launched", "reopen_requested"):
                    self.controller.publish(self.player_token, "player." + event["kind"], "PotPlayer 专用播放请求已发送。")
        if self.active is None:
            return
        if self.replacing_player:
            return
        state = data.get("state")
        if state == "closed":
            if not self.busy or not self.expected_absence:
                self.stop(stop_engine=False)
                self.events.emit("player.user_closed", "PotPlayer 窗口已关闭，直播与自动恢复已取消。", source="player")
            return
        if data.get("load_serial") != self.serial:
            return
        if self.active.get("kind") == "vod":
            if (self.active.get("content_type") == "pgc" and state == "ended" and not data.get("position_seconds")
                    and data.get("owned_source") and data.get("duration_seconds", 0) > 0
                    and self.position is not None and self.position >= data["duration_seconds"] - 2):
                data["position_seconds"] = data["duration_seconds"]
            if self.restore_target is not None:
                if data.get("state") in ("playing", "paused") and data.get("width", 0) > 0 and abs((data.get("position_seconds") or 0) - self.restore_target) <= 8:
                    self.controller.publish(self.player_token, "vod.position_restored", f"已恢复到约 {data.get('position_seconds', 0):.1f} 秒。")
                    self.restore_target = None
                elif time.monotonic() > self.restore_deadline:
                    self.controller.retry.stop();self.controller.fail(self.player_token, ViewerError("播放器未能恢复所需位置，请取消续播后重试。", code="vod.resume_failed"))
                    self.restore_target = None
                    return
            self.save_video_progress(data)
        if isinstance(data.get("volume"), (int, float)):
            self.config["volume"] = min(100, max(0, data["volume"]))
        if type(data.get("mute")) is bool:
            self.config["mute"] = data["mute"]
        if data.get("source_present") and not data.get("owned_source"):
            self.stop(stop_engine=False)
            self.events.emit("player.source_changed", "播放器已打开其他内容，直播自动恢复已停止。",
                             source="player", level="WARN", presentation="banner", scope="player")
            return
        if state in ("playing", "paused", "ended"):
            self.had_graph = True
        if state == "closed_media" and self.had_graph and not (self.busy and self.expected_closed_media):
            if self.active.get("kind") == "vod" and self.controller.state == "error":
                return
            if self.active.get("replay_mode") and self.relay is not None and getattr(self.relay, "connecting_until", 0) > time.monotonic():
                return
            if self.active.get("replay_mode") and self.resume_pending_at is not None and time.monotonic() - self.resume_pending_at < 5:
                return
            if self.active.get("replay_mode") and not self.busy:
                if self.media_closed_at is None:
                    self.media_closed_at = time.monotonic()
                    return
                if time.monotonic() - self.media_closed_at < 1:
                    return
            self.stop(stop_engine=False)
            self.events.emit("player.user_stopped", "已在 PotPlayer 关闭播放，自动恢复已取消。", source="player")
            return
        if state in ("playing", "paused"):
            self.media_closed_at = None
        if state == "paused":
            if self.resume_pending_at is not None and time.monotonic() - self.resume_pending_at < 5:
                return
            if self.busy and self.expected_pause:
                return
            if not self.paused:
                self.paused = True
                self.jobs.cancel("playback.prepare")
                self.controller.pause()
            return
        if self.paused and state in ("playing", "ended"):
            if self.active.get("replay_mode") or self.active.get("kind") == "vod":
                self.paused = self.controller.retry.paused = False
                self.controller.publish(self.token, "player.user_resumed", "继续从暂停位置播放。",
                                        state="connecting")
            else:
                self.events.emit("player.user_resumed", "继续直播，正在连接最新位置。", source="player")
                self.play(self.active)
                return
        if state == "playing":
            self.resume_pending_at = None
        if self.busy or not self.controller.current(self.player_token):
            return
        now = time.monotonic()
        position = data.get("position_seconds")
        if state == "playing" and position is not None and position != self.position:
            previous = self.position
            self.position, self.progress_at = position, now
            if previous is not None and position > previous and data.get("width", 0) > 0 and data.get("height", 0) > 0:
                if self.active.get("kind") == "vod":self.controller.connection_seen = True
                fault = self.events.notice_for("playback")
                pending_source_fault = fault is not None and (fault.code in ("relay.interrupted", "relay.unavailable") or
                    self.active.get("kind") == "vod" and (fault.impact == "interrupted" or fault.code == "vod.resume_failed"))
                if not pending_source_fault and self.restore_target is None:
                    retry.ready()
                    self.controller.recovered(self.player_token)
                    if self.controller.state != "playing":
                        self.controller.publish(self.player_token, "player.progress_verified",
                            "PotPlayer 报告画面尺寸与持续播放进度；请确认画面和声音。", state="playing")
        stalled = state == "playing" and self.progress_at is not None and now - self.progress_at > 15
        if self.active.get("kind") == "vod":
            if state == "ended" and self.controller.state not in ("ended", "error"):
                duration = data.get("duration_seconds") or 0
                if duration > 0 and (position or 0) >= duration - 2:
                    retry.stop()
                    self.controller.publish(self.player_token, "vod.ended", "当前集播放完毕，可拖动回看或选择其他剧集。" if self.active.get("content_type") == "pgc" else "当前分 P 播放完毕，可拖动回看或选择其他分 P。", state="ended")
                else:
                    retry.stop()
                    self.controller.fail(self.player_token, ViewerError("视频尚未播完，输入已结束，请重新获取视频。", code="vod.ended_early"))
            elif stalled and self.controller.state not in ("error", "reconnecting"):
                self.controller.fail(self.player_token, ViewerError("视频播放持续停滞，正在尝试恢复。", code="vod.stalled"), recovering=retry.active)
                if retry.active:self._schedule_retry()
            return
        if (state == "ended" or stalled) and retry.active and self.config["auto_reconnect"]:
            if self.controller.state not in ("reconnecting", "error", "unavailable"):
                self.controller.fail(self.player_token, ViewerError("直播连接结束，正在重新连接。",
                                     code="player.interrupted"), recovering=True)
            self._schedule_retry()

    def tick(self):
        if self.closed:
            return
        self._poll_player()  # User stop/close wins over an already queued worker result.
        self.jobs.drain()
        retry = self.controller.retry
        if (self.config["auto_reconnect"] and not self.busy and self.controller.state == "reconnecting"
                and retry.active and not self.paused):
            self._schedule_retry()
        if not self.config["auto_reconnect"]:
            retry.stop()
            if self.controller.state == "reconnecting" and not self.busy:
                self.controller.publish(self.token, "playback.auto_reconnect_disabled",
                    "自动恢复已关闭；直播中断时可手动回到直播。", state="error")
        elif not self.busy and self.active is not None and retry.consume():
            self.play(self.active, automatic=True)
        if self.active and self.active.get("replay_mode") and time.monotonic() - self.last_resource_check > 10:
            self.last_resource_check = time.monotonic()
            memory = available_memory()
            if memory and memory.get("free_commit_mb", 99999) < 512:
                self.stop()
                if self.player is not None:
                    self.player.close_window()
                self.events.emit("cache.memory_low", "可用内存不足，已结束回看会话释放资源；请缩短回看范围后重试。",
                                 source="player", level="ERROR", presentation="strong", impact="interrupted", scope="cache")
        if self.active and self.active.get("replay_mode") and time.monotonic() - self.last_resource_check > 10:
            self.last_resource_check = time.monotonic()
            memory = available_memory()
            if memory and memory.get("free_commit_mb", 99999) < 512:
                self.stop()
                if self.player is not None:
                    self.player.close_window()
                self.events.emit("cache.memory_low", "可用内存不足，已结束回看会话释放资源；请缩短回看范围后重试。",
                                 source="player", level="ERROR", presentation="strong", impact="interrupted", scope="cache")
        if self.relay is not None:
            if self.active.get("kind") == "vod" and hasattr(self.relay, "source_info"):
                self.stream = self.relay.source_info
                self.active["page"] = self.stream.page["page"]
            stats = self.relay.snapshot()
            if not self.history_recorded and stats["bytes_forwarded"] > 0:
                request = self.active
                if request.get("content_type") == "pgc":
                    from .bangumi_state import remember, saved_name
                    ep = self.stream.episode
                    record = {**ep.summary(), "name": saved_name(self.config, ep.season_id, self.stream.season_title), "season_title": self.stream.season_title}
                    self.config["bangumi_history"] = remember(self.config["bangumi_history"], record)
                    self.config["last_bangumi"] = record
                    for favorite in self.config["bangumi_favorites"]:
                        if favorite["season_id"] == ep.season_id:favorite["last_ep_id"] = ep.ep_id
                elif request.get("kind") == "vod":
                    record = {"bvid": self.stream.video.bvid, "page": self.stream.page["page"], "cid": self.stream.page["cid"], "name": request["room_name"]}
                    self.config["video_history"] = remember_video(self.config["video_history"], record)
                    self.config["last_video"] = record
                else:
                    record = {"platform": request["platform"], "room": request["room_input"], "name": request["room_name"]}
                    self.config["history"] = remember_play(self.config["history"], record)
                self.history_recorded = True
                self.persist()
                self.events.emit("vod.history_recorded" if request.get("kind") == "vod" else "history.recorded",
                                 "已记录本次观看视频。" if request.get("kind") == "vod" else "已记录本次观看房间。",
                                 source="vod" if request.get("kind") == "vod" else "history", target=self.token.target)
            if time.monotonic() - self.last_stats_at > 60:
                self.last_stats_at = time.monotonic()
                self.controller.publish(self.token, "relay.statistics",
                    f"已转发 {stats['bytes_forwarded'] / 1048576:.1f} MB；连接 {stats['connections']} 次。",
                    level="DEBUG", details={"event_count": stats["failures"]})

    def close(self):
        """Run in a shutdown worker so even slow player cleanup cannot freeze Qt."""
        self.closed = True
        self.stop()
        self.jobs.close()
        self.endpoint.close()
        self.replay_endpoint.close()
        if self.player is not None:
            self.player.close(wait=True)
        for thread in self.retiring:
            thread.join(timeout=2)
