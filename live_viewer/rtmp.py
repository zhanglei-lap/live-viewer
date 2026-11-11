"""Loopback RTMP transport to engage PotPlayer's native stream timeshift.

FLV tags are forwarded without decoding or re-encoding. Each playback request
captures one relay; refreshing its upstream preserves the native connection and
continues the media timestamps. Switching rooms creates a separate session.
"""
from __future__ import annotations

import os
import secrets
import socket
import struct
import threading
import time

import requests
from .core import ViewerError
from .network import configure_session
from .relay import LocalRelay


def amf(value):
    if value is None: return b"\x05"
    if isinstance(value, bool): return b"\x01" + bytes([value])
    if isinstance(value, (int, float)): return b"\x00" + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        if len(raw) > 65535: raise ValueError("AMF string too large")
        return b"\x02" + len(raw).to_bytes(2, "big") + raw
    if isinstance(value, dict):
        return b"\x03" + b"".join(len(k.encode()).to_bytes(2, "big") + k.encode() + amf(v)
                                 for k, v in value.items()) + b"\x00\x00\x09"
    raise ValueError("Unsupported AMF value")


def decode_amf(payload):
    def read(at, depth=0):
        if depth > 5 or at >= len(payload): raise ValueError("Invalid AMF")
        kind, at = payload[at], at + 1
        if kind == 0:
            return struct.unpack(">d", payload[at:at + 8])[0], at + 8
        if kind == 1: return bool(payload[at]), at + 1
        if kind in (5, 6): return None, at
        if kind == 2:
            size = int.from_bytes(payload[at:at + 2], "big");at += 2
            if size > 8192 or at + size > len(payload): raise ValueError("Invalid AMF string")
            return payload[at:at + size].decode("utf-8"), at + size
        if kind in (3, 8):
            if kind == 8: at += 4
            values = {}
            for _ in range(128):
                if payload[at:at + 3] == b"\x00\x00\x09": return values, at + 3
                size = int.from_bytes(payload[at:at + 2], "big");at += 2
                if size > 1024: raise ValueError("Invalid AMF key")
                key = payload[at:at + size].decode("utf-8");at += size
                values[key], at = read(at, depth + 1)
            raise ValueError("Large AMF object")
        raise ValueError("Unsupported AMF type")
    values, at = [], 0
    while at < len(payload) and len(values) < 16:
        value, at = read(at);values.append(value)
    return values


class Peer:
    def __init__(self, sock):
        self.sock, self.chunk = sock, 128
        self.states = {}
        self.lock = threading.Lock()
        self.idle_retry = False

    def read(self, size):
        data = bytearray()
        while len(data) < size:
            try:
                part = self.sock.recv(size - len(data))
            except socket.timeout:
                if self.idle_retry: continue
                raise
            if not part: raise EOFError()
            data.extend(part)
        return bytes(data)

    def receive(self):
        while True:
            basic = self.read(1)[0];fmt, cid = basic >> 6, basic & 63
            if cid == 0: cid = 64 + self.read(1)[0]
            elif cid == 1:
                ext = self.read(2);cid = 64 + ext[0] + 256 * ext[1]
            old = self.states.get(cid)
            if fmt == 0:
                header = self.read(11);stamp = int.from_bytes(header[:3], "big")
                state = {"stamp": stamp, "length": int.from_bytes(header[3:6], "big"), "type": header[6],
                         "stream": int.from_bytes(header[7:], "little"), "buffer": bytearray(), "ext": stamp == 0xffffff}
                if len(self.states) >= 32 and cid not in self.states: raise ValueError("Many chunk streams")
                self.states[cid] = state
            elif fmt in (1, 2):
                if old is None: raise ValueError("Missing chunk header")
                header = self.read(7 if fmt == 1 else 3);delta = int.from_bytes(header[:3], "big")
                state = {**old, "stamp": old["stamp"] + delta, "buffer": bytearray(), "ext": delta == 0xffffff}
                if fmt == 1: state.update(length=int.from_bytes(header[3:6], "big"), type=header[6])
                self.states[cid] = state
            else:
                if old is None: raise ValueError("Missing continuation")
                state = old
                if len(state["buffer"]) == state["length"]: state["buffer"] = bytearray()
            if state["ext"]: state["stamp"] = int.from_bytes(self.read(4), "big")
            if state["length"] > 2 * 1024 * 1024: raise ValueError("Large RTMP message")
            state["buffer"].extend(self.read(min(self.chunk, state["length"] - len(state["buffer"]))))
            if len(state["buffer"]) == state["length"]:
                if state["type"] == 1:
                    self.chunk = int.from_bytes(state["buffer"], "big") & 0x7fffffff
                    if not 1 <= self.chunk <= 65536: raise ValueError("Invalid chunk size")
                return state["type"], bytes(state["buffer"]), state["stream"]

    def send(self, kind, payload, stamp=0, stream=0, cid=3):
        stamp = max(0, int(stamp)) & 0xffffffff
        header = (bytes([cid]) + min(stamp, 0xffffff).to_bytes(3, "big") + len(payload).to_bytes(3, "big")
                  + bytes([kind]) + stream.to_bytes(4, "little"))
        extended = stamp.to_bytes(4, "big") if stamp >= 0xffffff else b""
        chunks = [header + extended + payload[:4096]]
        for at in range(4096, len(payload), 4096):
            chunks.append(bytes([0xc0 | cid]) + extended + payload[at:at + 4096])
        with self.lock: self.sock.sendall(b"".join(chunks))

    def command(self, name, tx=0, *args, stream=0):
        self.send(20, amf(name) + amf(tx) + b"".join(amf(arg) for arg in args), stream=stream)


def flv_tags(parts):
    buffer, header = bytearray(), True
    for part in parts:
        buffer.extend(part)
        if header:
            if len(buffer) < 13: continue
            if buffer[:3] != b"FLV": raise ValueError("Not FLV")
            offset = int.from_bytes(buffer[5:9], "big") + 4
            if not 13 <= offset <= 1024: raise ValueError("Invalid FLV header")
            if len(buffer) < offset: continue
            del buffer[:offset];header = False
        while len(buffer) >= 15:
            kind = buffer[0];length = int.from_bytes(buffer[1:4], "big")
            if length > 2 * 1024 * 1024: raise ValueError("Large FLV tag")
            if len(buffer) < length + 15: break
            if int.from_bytes(buffer[11 + length:15 + length], "big") != length + 11:
                raise ValueError("Invalid FLV tag size")
            stamp = int.from_bytes(buffer[4:7], "big") + (buffer[7] << 24)
            payload = bytes(buffer[11:11 + length]);del buffer[:length + 15]
            if kind in (8, 9, 18): yield kind, payload, stamp


class RtmpEndpoint:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.socket = self.thread = self.current = None
        self.connections = set()
        self.closed = False
        self.last_stage = "idle"

    @property
    def url(self):
        self.prepare()
        return f"rtmp://127.0.0.1:{self.socket.getsockname()[1]}/{self.token}/live.flv"

    def prepare(self):
        with self.lock:
            if self.closed: raise ViewerError("本机回看会话已关闭。", code="playback.cancelled", level="DEBUG")
            if self.socket is None:
                self.socket = socket.socket()
                self.socket.bind(("127.0.0.1", 0));self.socket.listen(4);self.socket.settimeout(.2)
                self.thread = threading.Thread(target=self._accept, name="loopback-rtmp", daemon=True)
                self.thread.start()

    def attach(self, relay):
        self.prepare()
        with self.lock:
            if self.current is not None and self.current is not relay and not self.current.stop_event.is_set():
                raise ViewerError("旧回看会话尚未停止。", code="relay.replacement_blocked")
            self.current = relay

    def detach(self, relay):
        with self.lock:
            if self.current is relay: self.current = None

    def _accept(self):
        while not self.closed:
            try: connection, _ = self.socket.accept()
            except socket.timeout: continue
            except OSError: break
            with self.lock:
                if len(self.connections) >= 4:
                    connection.close();continue
                self.connections.add(connection)
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        relay = None
        acquired = False
        try:
            connection.settimeout(8)
            peer = Peer(connection)
            self.last_stage = "handshake"
            hello = peer.read(1537)
            if hello[0] != 3: return
            connection.sendall(b"\x03" + b"\0" * 8 + os.urandom(1528) + hello[1:])
            peer.read(1536)
            authorised = False
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not self.closed:
                kind, payload, sid = peer.receive()
                if kind not in (20, 17): continue
                values = decode_amf(payload[1:] if kind == 17 else payload)
                name, tx = values[0], values[1]
                self.last_stage = str(name) if name in ("connect", "createStream", "getStreamLength", "play") else "command"
                if name == "connect":
                    if not isinstance(values[2], dict) or values[2].get("app") != self.token:
                        self.last_stage = "connect_rejected"
                        return
                    authorised = True
                    peer.send(5, (5000000).to_bytes(4, "big"))
                    peer.send(6, (5000000).to_bytes(4, "big") + b"\x02")
                    peer.send(1, (4096).to_bytes(4, "big"))
                    peer.command("_result", tx, {"fmsVer": "FMS/3,5,7,7009", "capabilities": 31},
                                 {"level": "status", "code": "NetConnection.Connect.Success",
                                  "description": "Local live stream", "objectEncoding": 0})
                elif not authorised: return
                elif name == "createStream": peer.command("_result", tx, None, 1)
                elif name == "getStreamLength": peer.command("_result", tx, None, 0)
                elif name in ("releaseStream", "FCPublish"): peer.command("_result", tx, None, None)
                elif name == "play":
                    if len(values) < 4 or values[3] not in ("live.flv", "live", "flv:live", "flv:live.flv"):
                        self.last_stage = "play_rejected"
                        return
                    with self.lock: relay = self.current
                    if relay is None or relay.stop_event.is_set() or not relay.active.acquire(blocking=False): return
                    acquired = True
                    relay.connecting_until = time.monotonic() + 15
                    self.last_stage = "streaming"
                    peer.send(4, b"\0\0" + (1).to_bytes(4, "big"))
                    for code in ("NetStream.Play.Reset", "NetStream.Play.Start"):
                        peer.command("onStatus", 0, None, {"level": "status", "code": code, "description": "Local live"}, stream=1)
                    peer.send(18, amf("|RtmpSampleAccess") + amf(True) + amf(True), stream=1)
                    break
            else: return
            relay.connection = connection
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Native DVR may stop consuming the socket while reading history.
            # Close/stop explicitly shuts down this socket, so blocking writes
            # remain cancellable without treating a valid rewind as a failure.
            connection.settimeout(None)
            peer.idle_retry = True
            reader_done = threading.Event()
            def controls():
                try:
                    while not self.closed and not relay.stop_event.is_set():
                        kind, payload, sid = peer.receive()
                        if kind == 4 and payload[:2] == b"\0\x06":
                            peer.send(4, b"\0\x07" + payload[2:])
                except (OSError, EOFError, ValueError):
                    reader_done.set()
                    relay.disconnect()
            threading.Thread(target=controls, daemon=True).start()
            offset, last_stamp, empty_attempts = 0, 0, 0
            while not relay.stop_event.is_set() and not reader_done.is_set():
                origin, delivered = None, 0
                for kind, payload, stamp in flv_tags(relay.source()):
                    if relay.stop_event.is_set() or reader_done.is_set(): break
                    media = kind in (8, 9) and len(payload) > 1 and payload[1] == 1
                    if origin is None and media: origin = stamp
                    relative = (stamp - origin) & 0xffffffff if origin is not None and stamp else 0
                    if relative >= 2**31: relative = 0
                    timestamp = offset + relative
                    peer.send(kind, payload, timestamp, 1, 6 if kind == 9 else 4 if kind == 8 else 5)
                    last_stamp = max(last_stamp, timestamp)
                    delivered += 1
                if relay.stop_event.is_set() or reader_done.is_set() or relay.source_interrupted.is_set(): break
                empty_attempts = 0 if delivered else empty_attempts + 1
                if empty_attempts >= 3: break
                offset = last_stamp + 40
                relay.http._emit("relay.cache_input_refreshed", "正在刷新直播输入，原生回看会话保持连接。",
                                 level="INFO")
                if relay.stop_event.wait(.2): break
            if not relay.stop_event.is_set() and not reader_done.is_set():
                relay.source_interrupted.set()
                relay.http._emit("relay.interrupted", "直播输入已中断，正在保留旧画面等待恢复。")
                while not relay.stop_event.is_set() and not reader_done.wait(.1):
                    pass  # Keep the old graph alive while a new source is prepared.
        except (OSError, EOFError, ValueError, IndexError, struct.error):
            self.last_stage = "protocol_closed"
        finally:
            if relay is not None and acquired:
                if relay.connection is connection:
                    relay.connection = None
                relay.active.release()
            with self.lock: self.connections.discard(connection)
            connection.close()

    def close(self):
        with self.lock:
            self.closed = True
            connections = tuple(self.connections)
            current = self.current
        if current is not None: current.stop()
        for connection in connections:
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        if self.socket is not None: self.socket.close()
        if self.thread is not None: self.thread.join(1)


class ReplayRelay:
    def __init__(self, *args, endpoint=None, replay_endpoint=None, **kwargs):
        # Native seeking can briefly stop network consumption. The three-second
        # realtime FIFO guard must not tear down a valid timeshift reader.
        kwargs["low_latency"] = False
        callback = kwargs.pop("on_event", None)
        self.source_interrupted = threading.Event()
        def on_event(code, message, **fields):
            if code == "relay.interrupted" and not self.source_interrupted.is_set():
                code, message = "relay.cache_input_refreshing", "直播输入连接结束，正在刷新地址并保持回看会话。"
                fields = {"level": "WARN", "impact": "degraded"}
            if callback: callback(code, message, **fields)
        self.http = LocalRelay(*args, endpoint=endpoint, on_event=on_event, **kwargs)
        self.endpoint = replay_endpoint
        self.stop_event = self.http.stop_event
        self.active = threading.Lock()
        self.connection = None
        self.connecting_until = 0.0

    def __getattr__(self, name):
        return getattr(self.http, name)

    @property
    def url(self):
        return self.endpoint.url

    def start(self):
        self.http.start()
        self.endpoint.attach(self)

    def source(self):
        session = configure_session(requests.Session())
        try:
            with session.get(self.http.url, stream=True, timeout=(5, 15)) as response:
                response.raise_for_status()
                for chunk in response.iter_content(8192):
                    if self.source_interrupted.is_set(): break
                    yield chunk
        except requests.RequestException:
            return
        finally: session.close()

    def disconnect(self):
        connection = self.connection
        if connection is not None:
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass

    def disconnect_upstream(self):
        self.source_interrupted.set()
        self.http._emit("relay.interrupted", "直播输入受控中断，等待重新连接。")

    def stop(self):
        self.stop_event.set()
        self.endpoint.detach(self)
        self.disconnect()
        self.http.stop()
