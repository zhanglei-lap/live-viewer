from __future__ import annotations

import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from .core import PlatformClient, ViewerError, open_media
from .live_buffer import LiveBuffer, RelayBacklog
from .timing import FlvClock


class RelayEndpoint:
    """Stable listener; each GET captures one immutable upstream attempt."""
    def __init__(self):
        self.path = "/" + secrets.token_urlsafe(24) + "/live.flv"
        self.lock = threading.RLock()
        self.server = self.thread = None
        self.closed = False

    def prepare(self, handler):
        with self.lock:
            if self.closed:
                raise ViewerError("本机播放会话已关闭。", code="playback.cancelled", level="DEBUG")
            if self.server is None:
                self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                self.server.daemon_threads = True
                self.server.current_relay = None
                self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
            return self.server, self.thread

    def attach(self, relay):
        with self.lock:
            if self.closed:
                raise ViewerError("本机播放会话已关闭。", code="playback.cancelled", level="DEBUG")
            current = self.server.current_relay
            if current is not None and current is not relay and not current.stop_event.is_set():
                raise ViewerError("旧直播流尚未停止，已取消替换。", code="relay.replacement_blocked")
            self.server.current_relay = relay
            if self.thread.ident is None:
                self.thread.start()

    def detach(self, relay):
        with self.lock:
            if self.server is not None and self.server.current_relay is relay:
                self.server.current_relay = None

    def close(self):
        with self.lock:
            self.closed = True
            server, thread = self.server, self.thread
            current = server.current_relay if server is not None else None
        if current is not None:
            current.stop()
        if server is not None:
            if thread.ident is not None:
                server.shutdown()
            server.server_close()


class LocalRelay:
    """One playback session. A player reconnect resolves a fresh upstream URL."""

    def __init__(self, platform, room, requested, cookies=None, *, low_latency=True, network_mode="direct",
                 initial_client=None, initial_stream=None, on_event=None, endpoint=None):
        self.platform, self.room, self.requested = platform, room, requested
        self.cookies = cookies.copy() if cookies is not None else None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.active = threading.Lock()
        self.response = None
        self.buffer = None
        self.last_buffer_stats = {}
        self.low_latency = low_latency
        self.network_mode = network_mode
        self.initial_client, self.initial_stream = initial_client, initial_stream
        self.initial_media = None
        self.on_event = on_event
        self.endpoint = endpoint
        self.input_clock, self.output_clock = FlvClock(), FlvClock()
        self.last_write_max = 0.0
        self.timing_ended_at = None
        self.started = time.monotonic()
        self.stats = {"connections": 0, "bytes_forwarded": 0, "failures": 0,
                      "status": "等待播放器连接", "last_stream": None,
                      "decoded_verified": False, "reconnect_recommended": False,
                      "slow_reader_disconnects": 0, "low_latency": low_latency, "network_mode": network_mode}
        self.path = endpoint.path if endpoint is not None else "/" + secrets.token_urlsafe(24) + "/live.flv"

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_):
                pass  # The local session token and URLs must not enter logs.

            def do_HEAD(self):
                relay = self.server.current_relay
                if relay is None or relay.stop_event.is_set():
                    self.send_error(503, "Stream not ready")
                    return
                if self.path != relay.path:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.send_header("Connection", "close")
                self.end_headers()

            def do_GET(self):
                relay = self.server.current_relay
                if relay is None:
                    self.send_error(503, "Stream not ready")
                    return
                if self.path != relay.path or relay.stop_event.is_set():
                    self.send_error(404)
                    return
                if not relay.active.acquire(blocking=False):
                    self.send_error(409)
                    return
                sent = False
                failed_after_start = False
                player_left = False
                with relay.lock:
                    client, prepared = relay.initial_client, relay.initial_stream
                    media = relay.initial_media
                    relay.initial_client = relay.initial_stream = None
                    relay.initial_media = None
                    relay.stats["status"] = "查询房间 / 播放接口" if client is None else "连接 CDN"
                client = client or PlatformClient(relay.platform, relay.cookies, network_mode=relay.network_mode)
                try:
                    stream = prepared or client.resolve(relay.room, relay.requested)
                    with relay.lock:
                        relay.stats["last_stream"] = stream.summary()
                        relay.stats["connections"] += 1
                        relay.stats["status"] = "连接 CDN"
                        relay.stats["reconnect_recommended"] = False
                        relay.input_clock, relay.output_clock = FlvClock(), FlvClock()
                        relay.last_write_max = 0.0
                        relay.timing_ended_at = None
                        relay.last_buffer_stats = {}
                    last_error = "CDN 连接或读取失败"
                    for url in ([None] if media is not None else client.media_urls(stream, on_event=relay._emit)):
                        response = None
                        pipe = None
                        reader = None
                        try:
                            if media is not None:
                                response, chunks, head = media
                                media = None
                            else:
                                response = open_media(client.session, url, relay.platform)
                                chunks = response.iter_content(8192)
                                head = next(chunks, b"")
                            with relay.lock:
                                relay.response = response
                            if head[:3] != b"FLV":
                                raise ViewerError("当前返回内容不是 FLV。")
                            if relay.stop_event.is_set():
                                break
                            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                            if relay.low_latency:
                                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                                self.connection.settimeout(3.0)
                            self.send_response(200)
                            self.send_header("Content-Type", "video/x-flv")
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            sent = True
                            with relay.lock:
                                relay.input_clock.feed(head)
                            relay._write_chunk(self, head)
                            with relay.lock:
                                relay.stats["status"] = "正在转发；请在播放器确认画面与声音"
                            relay._emit("relay.forwarding", "正在转发直播；请在播放器确认画面与声音。")
                            if relay.low_latency:
                                pipe = LiveBuffer()
                                with relay.lock:
                                    relay.buffer = pipe

                                def receive(upstream=response, source=chunks, fifo=pipe):
                                    error = None
                                    try:
                                        for chunk in source:
                                            if relay.stop_event.is_set() or fifo.cancelled:
                                                break
                                            with relay.lock:
                                                if relay.buffer is not fifo or fifo.cancelled:
                                                    break
                                                relay.input_clock.feed(chunk)
                                            fifo.put(chunk)
                                    except RelayBacklog as exc:
                                        error = exc
                                    except (requests.RequestException, OSError):
                                        error = ViewerError("上游直播流中断，请点击“回到直播”。")
                                    except Exception:
                                        error = ViewerError("上游直播流读取失败，请点击“回到直播”。")
                                    finally:
                                        fifo.finish(error)
                                        upstream.close()

                                reader = threading.Thread(target=receive, daemon=True)
                                reader.start()
                                while not relay.stop_event.is_set():
                                    chunk = pipe.get()
                                    if chunk is None:
                                        break
                                    relay._write_chunk(self, chunk)
                            else:
                                for chunk in chunks:
                                    if relay.stop_event.is_set():
                                        break
                                    with relay.lock:
                                        relay.input_clock.feed(chunk)
                                    relay._write_chunk(self, chunk)
                            break
                        except (BrokenPipeError, ConnectionResetError):
                            player_left = True
                            if not relay.stop_event.is_set():
                                relay._emit("relay.player_disconnected", "播放器已断开本机直播连接。", level="INFO")
                            break
                        except TimeoutError:
                            last_error = str(RelayBacklog())
                            failed_after_start = sent
                            with relay.lock:
                                relay.stats["failures"] += 1
                                relay.stats["slow_reader_disconnects"] += 1
                                relay.stats["reconnect_recommended"] = True
                            if sent:
                                break
                        except (ViewerError, requests.RequestException, OSError) as exc:
                            if isinstance(exc, ViewerError):
                                last_error = str(exc)
                            with relay.lock:
                                relay.stats["failures"] += 1
                                if isinstance(exc, RelayBacklog):
                                    relay.stats["slow_reader_disconnects"] += 1
                                    relay.stats["reconnect_recommended"] = True
                            if sent:
                                failed_after_start = True
                                break  # Never splice another FLV into an existing connection.
                            relay._emit("relay.candidate_failed", last_error, level="WARN", impact="degraded")
                        finally:
                            if pipe is not None:
                                buffer_stats = pipe.snapshot()
                                pipe.stop()
                                with relay.lock:
                                    relay.last_buffer_stats = buffer_stats
                                    relay.buffer = None
                            if response is not None and reader is None:
                                response.close()
                            with relay.lock:
                                relay.response = None
                    if not sent and not relay.stop_event.is_set():
                        raise ViewerError("候选直播线路连接失败：" + last_error)
                    with relay.lock:
                        relay.stats["status"] = (last_error if failed_after_start else "连接结束，可点击播放重新解析") if not relay.stop_event.is_set() else "已停止"
                    if not relay.stop_event.is_set() and not player_left:
                        relay._emit("relay.interrupted", last_error if failed_after_start else
                                    "直播转发连接已结束；可点击回到直播重新连接。")
                except ViewerError as exc:
                    with relay.lock:
                        relay.stats["failures"] += 1
                        relay.stats["status"] = str(exc)
                    if not relay.stop_event.is_set():
                        relay._emit("relay.unavailable", str(exc))
                    if not sent:
                        try:
                            self.send_error(502, "Upstream stream unavailable")
                        except OSError:
                            pass
                finally:
                    with relay.lock:
                        relay.timing_ended_at = time.monotonic()
                    client.close()
                    relay.active.release()

        if endpoint is None:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.server.daemon_threads = True
            self.server.current_relay = self
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        else:
            self.server, self.thread = endpoint.prepare(Handler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}{self.path}"

    def start(self):
        if self.endpoint is not None:
            self.endpoint.attach(self)
        else:
            self.thread.start()

    def _emit(self, code, message, **fields):
        if self.on_event is not None:
            self.on_event(code, message, **fields)

    def prepare_first_source(self):
        """Check the first FLV bytes before asking a player to wait on HTTP."""
        client, stream = self.initial_client, self.initial_stream
        if client is None or stream is None:
            return
        last_error = "CDN 连接或读取失败"
        for url in client.media_urls(stream, on_event=self._emit):
            response = None
            try:
                response = open_media(client.session, url, self.platform)
                chunks = response.iter_content(8192)
                head = next(chunks, b"")
                if head[:3] != b"FLV":
                    raise ViewerError("当前返回内容不是 FLV。")
                self.initial_media = (response, chunks, head)
                self._emit("relay.source_ready", "已预检查直播 FLV 数据，可连接播放器。")
                return
            except ViewerError as exc:
                last_error = str(exc)
                self._emit("relay.candidate_failed", last_error, level="WARN", impact="degraded")
            except (requests.RequestException, OSError):
                last_error = "CDN 直播读取失败或超时"
                self._emit("relay.candidate_failed", last_error, level="WARN", impact="degraded")
            finally:
                if response is not None and self.initial_media is None:
                    response.close()
        raise ViewerError("候选直播线路连接失败：" + last_error, code="relay.no_available_source")

    def _write_chunk(self, handler, chunk):
        started = time.monotonic()
        try:
            handler.wfile.write(chunk)
            handler.wfile.flush()
        finally:
            with self.lock:
                self.last_write_max = max(self.last_write_max, time.monotonic() - started)
        with self.lock:
            self.output_clock.feed(chunk)
            self.stats["bytes_forwarded"] += len(chunk)

    def snapshot(self):
        with self.lock:
            stats = dict(self.stats)
            received = self.input_clock.snapshot(self.timing_ended_at)
            forwarded = self.output_clock.snapshot(self.timing_ended_at)
            gap = None
            if self.input_clock.raw_timestamp is not None and self.output_clock.raw_timestamp is not None:
                delta = (self.input_clock.raw_timestamp - self.output_clock.raw_timestamp) % 2**32
                if delta < 2**31:
                    gap = round(delta / 1000, 3)
            pipe = self.buffer
            timing = {"received_media_seconds": received["media_seconds"],
                      "forwarded_media_seconds": forwarded["media_seconds"],
                      "forwarded_clock_drift_seconds": forwarded["clock_drift_seconds"],
                      "stream_progress_gap_seconds": gap,
                      "max_write_seconds": round(self.last_write_max, 3),
                      "player_display_latency_measured": False,
                      **self.last_buffer_stats}
        if pipe is not None:
            timing.update(pipe.snapshot())
        stats["timing"] = timing
        stats["elapsed_seconds"] = round(time.monotonic() - self.started, 1)
        return stats

    def stop(self):
        self.stop_event.set()
        with self.lock:
            pipe = self.buffer
            prepared_client = self.initial_client
            prepared_media = self.initial_media
            self.initial_client = self.initial_stream = None
            self.initial_media = None
        if pipe is not None:
            pipe.stop()
        if prepared_client is not None:
            prepared_client.close()
        if prepared_media is not None:
            prepared_media[0].close()
        if self.endpoint is not None:
            self.endpoint.detach(self)
        else:
            if self.thread.ident is not None:
                self.server.shutdown()
            self.server.server_close()
        with self.lock:
            self.stats["status"] = "已停止"
        # Reads time out within 15 seconds; daemon handlers do not hold up app exit.

    def disconnect_upstream(self):
        """Controlled fault injection on this relay only; keep the listener alive."""
        with self.lock:
            pipe = self.buffer
        if pipe is None:
            raise ViewerError("当前没有可中断的转发读取。", code="diagnostic.no_active_stream")
        pipe.finish(ViewerError("直播读取中断，需要重新取得直播源。", code="relay.interrupted"))
