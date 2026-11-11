"""Per-session loopback byte-range service; never stores whole videos or cookies."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import re
import secrets
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from .core import ViewerError, new_session
from .vod import media_url

CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)")


def byte_range(value, size):
    if value is None:
        return None
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)
    if not match or not any(match.groups()) or len(value) > 80:
        raise ValueError("Only one byte range is supported")
    first, last = match.groups()
    if first:
        start, end = int(first), int(last) if last else size - 1
        end = min(end, size - 1)
    else:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Invalid suffix")
        start, end = max(0, size - suffix), size - 1
    if not 0 <= start <= end < size:
        raise ValueError("Unsatisfiable range")
    return start, end


class VodProxy:
    def __init__(self, source, *, network_mode="direct", on_event=None, refresher=None):
        self.source_info = source
        self.network_mode, self.on_event = network_mode, on_event
        self.refresher = refresher
        self.refresh_lock = threading.Lock()
        self.generation = self.refreshes = 0
        self.identities = {}
        self.stop_event = threading.Event()
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(8)
        self.responses, self.clients = set(), set()
        self.tracks = {"vod-video.mp4": source.selected_video, "vod-audio.m4a": source.selected_audio}
        self.sizes = {}
        self.preferred = {}
        self.bytes = self.connections = self.failures = 0
        self.started_at = time.monotonic()
        self.server = self.thread = None

    def _request(self, session, track, headers, *, allow_renew=True):
        generation = self.generation
        errors = []
        urls = list(track.urls)
        preferred = self.preferred.get(track.key)
        if preferred in urls:
            urls.remove(preferred);urls.insert(0, preferred)
        for initial in urls[:4]:
            url = initial
            try:
                for _ in range(4):
                    if self.stop_event.is_set():
                        raise ViewerError("点播已停止。", code="playback.cancelled", level="DEBUG")
                    response = session.get(media_url(url), headers=headers, stream=True, allow_redirects=False, timeout=(5, 15))
                    if response.status_code in (301, 302, 303, 307, 308):
                        try:
                            destination = media_url(urljoin(url, response.headers.get("Location", "")))
                        finally:
                            response.close()
                        url = destination
                        continue
                    if response.status_code in (401, 403, 410):
                        response.close()
                        raise ViewerError("视频地址已失效或访问被拒绝，请重新获取视频。", code="vod.url_expired")
                    if response.status_code not in (200, 206):
                        response.close()
                        raise ViewerError("视频 CDN 暂时无法读取，请重试。", code="vod.cdn_failed")
                    if "Range" in headers and response.status_code != 206:
                        response.close()
                        raise ViewerError("视频 CDN 未正确响应字节范围，已停止本次读取。", code="vod.range_unsupported")
                    self.preferred[track.key] = initial
                    return response
                raise ViewerError("视频 CDN 重定向次数过多。", code="vod.cdn_failed")
            except requests.RequestException:
                errors.append(ViewerError("视频 CDN 连接或读取失败，请检查网络后重试。", code="vod.network_failed"))
            except ViewerError as exc:
                if self.stop_event.is_set():
                    raise
                errors.append(exc)
            if self.on_event and errors and not self.stop_event.is_set():
                self.on_event("vod.candidate_failed", str(errors[-1]), level="WARN", impact="degraded",
                              details={"host": urlparse(initial).hostname or "", "stage": "点播 CDN"})
        error = errors[-1] if errors else ViewerError("没有可用视频线路。", code="vod.cdn_failed")
        if allow_renew and self.refresher is not None and error.code in ("vod.url_expired", "vod.network_failed", "vod.cdn_failed"):
            self.refresh(generation)
            updated = next((t for t in self.tracks.values() if t.key == track.key), None)
            if updated is not None:return self._request(session, updated, headers, allow_renew=False)
        raise error

    def _session(self):
        session = new_session(network_mode=self.network_mode)
        session.headers["Referer"] = "https://www.bilibili.com/video/" + self.source_info.video.bvid
        return session

    def prepare(self):
        self.sizes, self.identities = self._probe(self.tracks)
        return self

    def _probe(self, tracks):
        sizes, identities = {}, {}
        with self._session() as session:
            for name, track in tracks.items():
                with self._request(session, track, {"Range": "bytes=0-4095"}, allow_renew=False) as response:
                    match = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                    if not match or int(match[1]) != 0 or int(match[2]) != min(4095, int(match[3]) - 1) or int(match[3]) <= 15:
                        raise ViewerError("视频 CDN 字节范围信息无效。", code="vod.range_invalid")
                    sample = response.raw.read(int(match[2]) + 1)
                    if len(sample) != int(match[2]) + 1 or sample[4:8] not in (b"ftyp", b"styp"):
                        raise ViewerError("视频轨道未返回有效 MP4 数据。", code="vod.media_invalid")
                    sizes[name] = int(match[3])
                    identities[name] = hashlib.sha256(sample).hexdigest()
        return sizes, identities

    def refresh(self, expected_generation=None):
        with self.refresh_lock:
            if self.stop_event.is_set():raise ViewerError("点播已停止。", code="playback.cancelled", level="DEBUG")
            if expected_generation is not None and expected_generation != self.generation:return
            if self.refresher is None:raise ViewerError("当前会话不能刷新地址。", code="vod.url_expired")
            if self.on_event:self.on_event("vod.urls_refreshing", "正在刷新视频地址，保留当前内容和播放连接。", level="INFO")
            old = self.source_info
            fresh = self.refresher(old)
            if (fresh.video.bvid, fresh.page["cid"], fresh.selected_video.key, fresh.selected_audio.key) != (
                    old.video.bvid, old.page["cid"], old.selected_video.key, old.selected_audio.key):
                raise ViewerError("刷新后的内容或轨道已经变化，已停止恢复。", code="vod.media_changed")
            tracks = {"vod-video.mp4": fresh.selected_video, "vod-audio.m4a": fresh.selected_audio}
            sizes, identities = self._probe(tracks)
            if sizes != self.sizes or identities != self.identities:
                raise ViewerError("刷新后的媒体文件与原播放内容不一致，已停止恢复。", code="vod.media_changed")
            if self.stop_event.is_set():raise ViewerError("点播已停止。", code="playback.cancelled", level="DEBUG")
            fresh.subtitle, fresh.subtitle_text = old.subtitle, old.subtitle_text
            with self.lock:
                self.source_info, self.tracks = fresh, tracks
                self.generation += 1;self.refreshes += 1
            if self.on_event:self.on_event("vod.urls_refreshed", "视频地址已更新，原分 P 与播放进度保留。", level="INFO")

    @property
    def url(self):
        return self.address("vod-video.mp4")

    @property
    def audio_url(self):
        return self.address("vod-audio.m4a")

    def address(self, name):
        if self.server is None:
            raise ViewerError("点播服务尚未启动。", code="vod.service_unavailable")
        return f"http://127.0.0.1:{self.server.server_port}/{self.token}/{name}"

    def start(self):
        proxy = self
        class Server(ThreadingHTTPServer):
            def __init__(self, *args):
                self.workers = threading.BoundedSemaphore(16)
                super().__init__(*args)
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(5)
                with proxy.lock:
                    proxy.clients.add(connection)
                return connection, address
            def process_request(self, connection, address):
                if not self.workers.acquire(blocking=False):
                    with proxy.lock:proxy.clients.discard(connection)
                    connection.close();return
                try:
                    super().process_request(connection, address)
                except Exception:
                    self.workers.release()
                    with proxy.lock:proxy.clients.discard(connection)
                    connection.close()
                    raise
            def process_request_thread(self, connection, address):
                try:
                    super().process_request_thread(connection, address)
                finally:
                    with proxy.lock:proxy.clients.discard(connection)
                    self.workers.release()
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass  # Request paths contain the private session token.
            def handle(self):
                try:
                    super().handle()
                except (ConnectionError, socket.timeout):
                    pass
            def do_HEAD(self):
                self.serve(True)
            def do_GET(self):
                self.serve(False)
            def serve(self, head):
                name = self.path.removeprefix("/" + proxy.token + "/")
                if proxy.stop_event.is_set() or self.path != "/" + proxy.token + "/" + name or name not in proxy.tracks:
                    self.send_error(404, "Not found");return
                if not proxy.slots.acquire(blocking=False):
                    self.send_error(503, "Busy");return
                response = None
                sent = False
                with proxy.lock:
                    proxy.clients.add(self.connection)
                    proxy.connections += 1
                try:
                    self.connection.settimeout(15)
                    size = proxy.sizes[name]
                    try:
                        interval = byte_range(self.headers.get("Range"), size)
                    except ValueError:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.send_header("Connection", "close")
                        self.end_headers();return
                    start, end = interval if interval else (0, size - 1)
                    length = end - start + 1
                    if not head:
                        session = proxy._session()
                        try:
                            response = proxy._request(session, proxy.tracks[name], {"Range": f"bytes={start}-{end}"})
                            match = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                            if not match or tuple(map(int, match.groups())) != (start, end, size):
                                raise ViewerError("视频 CDN 返回的字节范围与请求不匹配。", code="vod.range_invalid")
                            with proxy.lock:
                                proxy.responses.add(response)
                            self.send_response(206 if interval else 200)
                            self.send_header("Content-Type", "video/mp4" if name == "vod-video.mp4" else "audio/mp4")
                            self.send_header("Accept-Ranges", "bytes")
                            self.send_header("Content-Length", str(length))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Connection", "close")
                            if interval:
                                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                            self.end_headers();sent = True
                            remaining, retries = length, 0
                            while remaining and not proxy.stop_event.is_set():
                                try:
                                    for chunk in response.iter_content(65536):
                                        if proxy.stop_event.is_set():break
                                        if len(chunk) > remaining:raise ViewerError("视频读取超出请求范围。", code="vod.range_invalid")
                                        self.wfile.write(chunk);remaining -= len(chunk)
                                        with proxy.lock:proxy.bytes += len(chunk)
                                    if remaining and not proxy.stop_event.is_set():raise ViewerError("视频读取提前结束。", code="vod.read_incomplete")
                                except (requests.RequestException, ViewerError) as exc:
                                    if proxy.stop_event.is_set():break
                                    if retries >= 2 or isinstance(exc, ViewerError) and exc.code != "vod.read_incomplete":raise
                                    retries += 1
                                    if proxy.on_event:proxy.on_event("vod.read_retry", "正在恢复中断的字节读取，保留当前播放位置。", level="WARN", impact="degraded")
                                    with proxy.lock:proxy.responses.discard(response)
                                    response.close()
                                    cursor = start + length - remaining
                                    response = proxy._request(session, proxy.tracks[name], {"Range": f"bytes={cursor}-{end}"})
                                    match = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                                    if not match or tuple(map(int, match.groups())) != (cursor, end, size):raise ViewerError("恢复的字节范围不匹配。", code="vod.range_invalid")
                                    with proxy.lock:proxy.responses.add(response)
                        finally:
                            if response is not None:
                                with proxy.lock:
                                    proxy.responses.discard(response)
                                response.close()
                            session.close()
                    else:
                        self.send_response(206 if interval else 200)
                        self.send_header("Content-Type", "video/mp4" if name == "vod-video.mp4" else "audio/mp4")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Length", str(length))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        if interval:
                            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                        self.end_headers();sent = True
                except (ConnectionError, socket.timeout):
                    pass  # Seeking or stopping cancels the player's prior reader.
                except (ViewerError, requests.RequestException) as exc:
                    if not proxy.stop_event.is_set():
                        with proxy.lock:
                            proxy.failures += 1
                        error = exc if isinstance(exc, ViewerError) else ViewerError("视频 CDN 读取失败。", code="vod.network_failed")
                        if proxy.on_event:
                            proxy.on_event(error.code, str(error), level=error.level, impact="interrupted", presentation="strong", dedupe_key="playback.failure")
                    if not sent:
                        self.send_error(502, "Media unavailable")
                    self.close_connection = True
                finally:
                    self.close_connection = True
                    with proxy.lock:
                        proxy.clients.discard(self.connection)
                    proxy.slots.release()
        if self.stop_event.is_set():
            raise ViewerError("点播已停止。", code="playback.cancelled", level="DEBUG")
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return {"status": "stopped" if self.stop_event.is_set() else "点播字节范围服务", "kind": "vod",
                    "bytes_forwarded": self.bytes, "connections": self.connections, "failures": self.failures,
                    "elapsed_seconds": round(time.monotonic() - self.started_at, 1), "last_stream": self.source_info.summary(), "url_refreshes": self.refreshes}

    def stop(self):
        self.stop_event.set()
        with self.lock:
            responses, clients = tuple(self.responses), tuple(self.clients)
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for response in responses:
            response.close()
        if self.server is not None:
            self.server.shutdown();self.server.server_close()
        if self.thread is not None:
            self.thread.join(1)
