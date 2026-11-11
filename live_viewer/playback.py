"""Playback business lifecycle without Tk, Qt, or a particular video engine."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from .core import PlatformClient, ViewerError
from .reconnect import ReconnectPolicy
from .relay import LocalRelay


def safe_error(exc):
    return exc if isinstance(exc, ViewerError) else ViewerError(
        "操作失败，请检查网络、播放器路径或接口兼容性。", code="operation.unexpected")


@dataclass(frozen=True)
class SessionToken:
    session_id: str
    generation: int
    target: str


@dataclass(frozen=True)
class OperationToken:
    channel: str
    generation: int
    target: str | None


class OperationController:
    """Independent account/diagnostic jobs are not invalidated by playback events."""
    def __init__(self):
        self.lock = threading.RLock()
        self.generations = {}
        self.closed = False

    def begin(self, channel, target=None):
        with self.lock:
            generation = self.generations.get(channel, 0) + 1
            self.generations[channel] = generation
            return OperationToken(channel, generation, target)

    def current(self, token):
        with self.lock:
            return not self.closed and self.generations.get(token.channel) == token.generation

    def cancel(self, channel):
        with self.lock:
            self.generations[channel] = self.generations.get(channel, 0) + 1

    def close(self):
        with self.lock:
            self.closed = True


class PlaybackController:
    def __init__(self, events):
        self.events = events
        self.lock = threading.RLock()
        self.token = None
        self.generation = 0
        self.request = None
        self.state = "idle"
        self.message = "尚未开始实播"
        self.incident = 0
        self.connection_seen = False
        self.closed = False
        self.retry = ReconnectPolicy()

    def begin(self, request, *, automatic=False):
        with self.lock:
            vod = request.get("kind") == "vod"
            target = ("vod:" if vod else "") + f"{request['platform']}:{request['room_input']}"
            previous = self.token
            if self.closed or automatic and (previous is None or previous.target != target or self.state == "paused"):
                raise ViewerError("过期的自动重连已取消。", code="playback.cancelled", level="DEBUG")
            same = automatic and previous is not None and previous.target == target
            self.generation += 1
            self.token = SessionToken(previous.session_id if same else str(uuid.uuid4()), self.generation, target)
            self.request = dict(request)
            if not same:
                self.incident = 0
                self.connection_seen = False
                self.events.clear("playback")
                self.events.reset_dedupe("playback")
            self.state = "reconnecting" if automatic else "resolving"
            self.message = "正在取得视频音轨并启动播放器…" if vod else ("正在重新连接直播…" if automatic else "正在解析直播流并启动播放器…")
            self.publish(self.token, "playback.retry_started" if automatic else "playback.start_requested",
                         self.message, details={"attempt": self.retry.total_attempts,
                                                "network_mode": request.get("network_mode", "direct")})
            return self.token

    def current(self, token):
        with self.lock:
            return not self.closed and token is not None and token == self.token

    def publish(self, token, code, message, *, state=None, **fields):
        with self.lock:
            if not self.current(token):
                self.events.emit("playback.stale_result", "已忽略停止、切换或重试前的过期播放事件。",
                                 source="playback", level="DEBUG", target=token.target if token else None,
                                 session_id=token.session_id if token else None, details={"reason": code})
                return False
            if state is not None:
                self.state, self.message = state, message
            vod = self.request is not None and self.request.get("kind") == "vod"
            if vod and code.startswith("playback."):
                code = "vod." + code.removeprefix("playback.")
            self.events.emit(code, message, source="pgc" if self.request and self.request.get("content_type") == "pgc" else "vod" if vod else "playback", target=token.target,
                             session_id=token.session_id, scope="playback", incident=self.incident, **fields)
            return True

    def fail(self, token, error, *, recovering=False):
        error = safe_error(error)
        with self.lock:
            offline = error.code == "room.offline"
            return self.publish(token, error.code, str(error), level=error.level,
                                impact="interrupted" if self.connection_seen else "start_blocked",
                                presentation="banner" if offline else "strong",
                                dedupe_key="playback.failure",
                                state="unavailable" if offline else ("reconnecting" if recovering else "error"))

    def relay_event(self, token, code, message, **fields):
        with self.lock:
            state = None
            if code.startswith(("vod.", "pgc.")) and fields.get("impact") == "interrupted":
                recoverable = code in ("vod.network_failed", "vod.cdn_failed", "vod.url_expired", "vod.read_incomplete", "pgc.network_failed")
                state = "paused" if self.retry.paused else "reconnecting" if self.retry.active and recoverable else "error"
                if not recoverable:self.retry.stop()
            if code in ("relay.interrupted", "relay.unavailable"):
                if self.state == "paused" or self.retry.paused:
                    self.publish(token, "relay.paused_stream_closed",
                                 "暂停期间旧直播连接已结束；继续时重新连接最新直播。")
                    return
                state = "reconnecting" if self.retry.active else "error"
                fields.update(level="ERROR", impact="interrupted" if self.connection_seen else "start_blocked", presentation="strong",
                              dedupe_key="playback.failure")
            elif code == "relay.forwarding":
                # Forwarding is evidence of transport, not of decoded picture/audio.
                state = "connecting"
                if self.current(token):
                    self.connection_seen = True
                self.recovered(token)
            self.publish(token, code, message, state=state, **fields)

    def recovered(self, token):
        with self.lock:
            if self.current(token):
                if self.events.notice_for("playback") is not None:
                    self.publish(token, "playback.recovered", "视频连接已恢复，播放位置保留。" if self.request.get("kind") == "vod" else "直播连接已恢复；请在播放器确认画面与声音。")
                    self.incident += 1
                self.events.clear("playback")
                self.events.reset_dedupe("playback")

    def decoded(self, token):
        with self.lock:
            if self.current(token) and self.state != "playing":
                self.recovered(token)
                self.publish(token, "playback.decoded", "已取得内置播放器的画面输出与播放进度。",
                             state="playing")

    def pause(self):
        with self.lock:
            if self.token is not None:
                self.retry.paused = True
                self.retry.due = None
                self.publish(self.token, "playback.paused", "已暂停，可从当前位置继续。" if self.request.get("kind") == "vod" else "已暂停；可继续当前播放或回到直播。", state="paused")

    def stop(self, *, closing=False):
        with self.lock:
            previous = self.token
            vod = self.request is not None and self.request.get("kind") == "vod"
            self.token, self.request = None, None
            self.generation += 1
            self.retry.stop()
            self.state, self.message = "stopped", "视频播放已停止。" if vod else "播放流已停止，自动重连已取消。"
            self.events.clear("playback")
            if previous is not None or not closing:
                self.events.emit("vod.stopped" if vod else "playback.stopped", self.message, source="vod" if vod else "playback",
                                 target=previous.target if previous else None,
                                 session_id=previous.session_id if previous else None)
            if closing:
                self.closed = True

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "message": self.message,
                    "session_id": self.token.session_id if self.token else None,
                    "target": self.token.target if self.token else None}


class PlaybackService:
    """Resolve/prepare the media in a worker; the GUI attaches its player later."""
    def __init__(self, controller, *, client_factory=PlatformClient, relay_factory=LocalRelay):
        self.controller = controller
        self.client_factory, self.relay_factory = client_factory, relay_factory

    def prepare(self, request, cookies, token, *, endpoint=None, replay_endpoint=None, start=True, cookie_provider=None):
        if request.get("kind") == "vod":
            if request.get("content_type") == "pgc":
                return self.prepare_bangumi(request, cookies, token, start=start, cookie_provider=cookie_provider)
            return self.prepare_video(request, cookies, token, start=start, cookie_provider=cookie_provider)
        client = self.client_factory(request["platform"], cookies, network_mode=request.get("network_mode", "direct"))
        relay = None
        try:
            if cookies is not None:
                login = client.login_status()
                if login["verified"] is False:
                    raise ViewerError(login["message"], code="account.expired", level="WARN")
            stream = client.resolve(request["room_input"], request["requested"])
            if not self.controller.current(token):
                raise ViewerError("播放启动已取消。", code="playback.cancelled", level="DEBUG")
            self.controller.publish(token, "playback.resolved", f"直播接口已解析；实际返回 {stream.actual_name}。")
            options = {"endpoint": endpoint} if endpoint is not None else {}
            factory = self.relay_factory
            if replay_endpoint is not None:
                from .rtmp import ReplayRelay
                factory = ReplayRelay
                options["replay_endpoint"] = replay_endpoint
            relay = factory(request["platform"], request["room_input"], request["requested"], cookies,
                                       low_latency=request["low_latency"], network_mode=request.get("network_mode", "direct"),
                                       initial_client=client, initial_stream=stream,
                                       on_event=lambda code, message, **fields: self.controller.relay_event(token, code, message, **fields),
                                       **options)
            relay.prepare_first_source()
            if not self.controller.current(token):
                raise ViewerError("播放启动已取消。", code="playback.cancelled", level="DEBUG")
            # No external-player launch occurs in this worker. A cancelled result
            # cannot accidentally start PotPlayer; only the accepted GUI callback can.
            if start:
                relay.start()
            return relay, stream
        except Exception:
            if relay is not None:
                relay.stop()
            else:
                client.close()
            raise

    def prepare_video(self, request, cookies, token, *, start=False, cookie_provider=None):
        from .vod import VodClient
        from .vod_proxy import VodProxy
        client = VodClient(cookies, network_mode=request.get("network_mode", "direct"))
        relay = None
        try:
            source = client.resolve(request["video_input"], request["page"], request.get("video_key"), request.get("audio_key"), cid=request.get("cid"))
            if request.get("cid") is not None and source.page["cid"] != request["cid"]:
                raise ViewerError("视频分 P 已发生变化，请重新获取视频后选择。", code="vod.part_changed", level="WARN")
            if not self.controller.current(token):
                raise ViewerError("视频启动已取消。", code="playback.cancelled", level="DEBUG")
            if request.get("subtitle_key"):
                from .subtitles import download
                subtitles, _ = client.subtitle_catalog(source.video.bvid, source.page["cid"])
                subtitle = next((s for s in subtitles if s.key == request["subtitle_key"]), None)
                if subtitle is None:raise ViewerError("所选字幕已不可用，请重新获取或选择不加载字幕。", code="vod.subtitle_missing", level="WARN")
                source.subtitle, source.subtitle_text = subtitle, download(subtitle, network_mode=request.get("network_mode", "direct"))
            def refresh(old):
                if not self.controller.current(token):raise ViewerError("播放会话已结束。", code="playback.cancelled", level="DEBUG")
                jar = cookie_provider() if cookie_provider is not None else cookies
                if request["session"] == "导入 Cookie" and jar is None:raise ViewerError("登录已清除，请重新登录或选择匿名。", code="account.missing", level="WARN")
                fresh_client = VodClient(jar, network_mode=request.get("network_mode", "direct"))
                try:
                    fresh = fresh_client.resolve(old.video.bvid, video_key=old.selected_video.key,
                                                 audio_key=old.selected_audio.key, cid=old.page["cid"])
                    if not self.controller.current(token):raise ViewerError("播放会话已结束。", code="playback.cancelled", level="DEBUG")
                    return fresh
                finally:fresh_client.close()
            self.controller.publish(token, "vod.resolved", f"P{source.page['page']}；实际返回 {source.actual_name}。")
            relay = VodProxy(source, network_mode=request.get("network_mode", "direct"),
                             on_event=lambda code, message, **fields: self.controller.relay_event(token, code, message, **fields), refresher=refresh)
            relay.prepare()
            expected = request.get("expected_media")
            if expected is not None and (relay.sizes != expected["sizes"] or relay.identities != expected["identities"]):
                raise ViewerError("恢复后的媒体文件已变化，已停止自动恢复。", code="vod.media_changed")
            if not self.controller.current(token):
                raise ViewerError("视频启动已取消。", code="playback.cancelled", level="DEBUG")
            if start:
                relay.start()
            return relay, source
        except Exception:
            if relay is not None:
                relay.stop()
            raise
        finally:
            client.close()

    def prepare_bangumi(self, request, cookies, token, *, start=False, cookie_provider=None):
        from .bangumi import BangumiClient, BangumiProxy
        route = request.get("network_mode", "direct")
        client = BangumiClient(cookies, network_mode=route)
        relay = None
        def current():
            if not self.controller.current(token):
                raise ViewerError("番剧播放已取消。", code="playback.cancelled", level="DEBUG")
        try:
            current()
            source = client.resolve(f"ep{request['ep_id']}", expected_cid=request.get("cid"),
                video_key=request.get("video_key"), audio_key=request.get("audio_key"), requested=request.get("requested", 127))
            if source.episode.season_id != request["season_id"]:
                raise ViewerError("该 EP 的季度已改变，请重新获取番剧。", code="pgc.identity_mismatch", level="WARN")
            current()
            if request.get("subtitle_key"):
                from .subtitles import download
                subtitles, _ = client.subtitle_catalog(source.episode)
                subtitle = next((s for s in subtitles if s.key == request["subtitle_key"]), None)
                if subtitle is None:raise ViewerError("所选字幕已不可用，请重新查询或关闭 CC 字幕。", code="pgc.subtitle_missing", level="WARN")
                source.subtitle, source.subtitle_text = subtitle, download(subtitle, network_mode=route)
            def refresh(old):
                current()
                jar = cookie_provider() if cookie_provider is not None else cookies
                if request["session"] == "导入 Cookie" and jar is None:
                    raise ViewerError("登录已清除，请重新登录后恢复番剧。", code="account.missing", level="WARN")
                fresh_client = BangumiClient(jar, network_mode=route)
                try:
                    fresh = fresh_client.resolve(f"ep{old.episode.ep_id}", expected_cid=old.episode.cid,
                        video_key=old.selected_video.key, audio_key=old.selected_audio.key, requested=old.requested)
                    current()
                    return fresh
                finally:fresh_client.close()
            self.controller.publish(token, "pgc.resolved", f"第 {source.episode.number} 集；实际返回 {source.actual_name}。")
            relay = BangumiProxy(source, network_mode=route, refresher=refresh,
                on_event=lambda code, message, **fields: self.controller.relay_event(token, code, message, **fields))
            relay.prepare()
            expected = request.get("expected_media")
            if expected is not None and (relay.sizes != expected["sizes"] or relay.identities != expected["identities"]):
                raise ViewerError("番剧媒体已经变化，已停止自动恢复。", code="vod.media_changed")
            current()
            if start:relay.start()
            return relay, source
        except Exception:
            if relay is not None:relay.stop()
            raise
        finally:client.close()
