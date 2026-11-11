from __future__ import annotations

import copy
import hashlib
import http.cookiejar
import json
import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from .network import configure_session, connection_message, connection_kind
from .platforms import PLATFORM_SPECS, PlatformSpec, require_spec

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
# 平台差异统一由 platforms.PLATFORM_SPECS 声明。这三个名字保留，供 Tk 前端、凭据存储与测试继续使用。
REFERERS = {key: spec.referer for key, spec in PLATFORM_SPECS.items()}
COOKIE_DOMAINS = {key: spec.cookie_domains for key, spec in PLATFORM_SPECS.items()}
MEDIA_DOMAINS = {key: spec.media_domains for key, spec in PLATFORM_SPECS.items()}


class ViewerError(Exception):
    """A message safe to display or export, without request URLs or credentials."""

    def __init__(self, message, *, code="operation.failed", level="ERROR"):
        super().__init__(message)
        self.code, self.level = code, level


def belongs_to(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower().lstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def room_number(platform: str, value: str) -> str:
    spec = require_spec(platform)
    limit = spec.max_room_digits
    value = value.strip()
    if value.isascii() and value.isdigit() and 0 < len(value) <= limit and int(value) > 0:
        return str(int(value))
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not belongs_to(parsed.hostname or "", spec.page_hosts):
        raise ViewerError("请输入该平台的房间号或直播间链接。")
    candidate = parsed.path.strip("/").split("/")[0]
    if spec.topic_query_room and parsed.path.startswith("/topic/"):
        candidate = parse_qs(parsed.query).get("rid", [""])[0]
    if not re.fullmatch(rf"[0-9]{{1,{limit}}}", candidate) or int(candidate) <= 0:
        raise ViewerError("目前仅支持数字房间号，请从直播间链接中确认房间号。")
    return str(int(candidate))


def missing_credentials(spec: PlatformSpec, jar) -> str:
    """返回该平台必需的、但 jar 中缺失的 cookie 名；空串表示齐全。"""
    return "、".join(name for name in spec.import_credentials
                     if not any(c.name == name and c.value for c in jar))


def load_cookies(path: str | Path, platform: str) -> requests.cookies.RequestsCookieJar:
    """Read only the selected platform's cookies; never persist the file or its path."""
    spec = require_spec(platform)
    path = Path(path)
    database_message = "这是 Edge/Chrome 的 Cookie 数据库，不能直接作为文本导入。请使用“粘贴 Cookie”，或导出 Netscape 格式文件；修改后缀无效。"
    try:
        with path.open("rb") as file:
            signature = file.read(16)
    except OSError:
        if path.name.lower() == "cookies" and path.parent.name.lower() == "network":
            raise ViewerError(database_message) from None
        raise ViewerError("无法读取文件，请选择本机可读的 Cookie 导出文件。") from None
    if signature == b"SQLite format 3\x00":
        raise ViewerError(database_message)
    source = http.cookiejar.MozillaCookieJar()
    try:
        source.load(str(path), ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError, ValueError):
        raise ViewerError("无法读取 Cookie 文件，请使用 Netscape cookies.txt 格式。") from None
    result = requests.cookies.RequestsCookieJar()
    for cookie in source:
        if cookie.expires == 0:
            cookie.expires = None  # Common Netscape exporters use 0 for session cookies.
        if belongs_to(cookie.domain, spec.cookie_domains) and not cookie.is_expired():
            result.set_cookie(copy.copy(cookie))
    if not result:
        raise ViewerError("文件中没有该平台未过期的 Cookie。")
    missing = missing_credentials(spec, result)
    if missing:
        raise ViewerError(f"文件中缺少 {spec.caption} {missing} 登录凭据，请在登录后重新导出。")
    return result


def cookies_from_text(text: str, platform: str) -> requests.cookies.RequestsCookieJar:
    """Import a user-pasted Cookie header into a platform-scoped, memory-only jar."""
    spec = require_spec(platform)
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if not text or len(text) > 65536 or any(c in text for c in ("\r", "\n", "\x00")):
        raise ViewerError("请输入单行 Cookie 内容。")
    if spec.paste_alias and ";" not in text and "=" not in text:
        text = spec.paste_alias + "=" + text
    jar = requests.cookies.RequestsCookieJar()
    for pair in text.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        name, separator, value = pair.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_!#$%&'*+.^`|~-]+", name):
            hint = f"{spec.caption}也可只粘贴 {spec.paste_alias} 的值。" if spec.paste_alias else ""
            raise ViewerError(f"格式应为 name=value; name2=value2。{hint}")
        jar.set(name, value, domain="." + spec.cookie_domains[0], path="/", secure=True)
    missing = missing_credentials(spec, jar)
    if missing:
        raise ViewerError(f"粘贴内容中缺少 {missing} 登录凭据。")
    if not any(c.value for c in jar):
        raise ViewerError("Cookie 内容为空。")
    return jar


def new_session(cookies=None, *, network_mode="direct") -> requests.Session:
    session = configure_session(requests.Session(), network_mode)
    session.headers.update({"User-Agent": UA, "Accept-Encoding": "identity"})
    if cookies is not None:
        for cookie in cookies:
            session.cookies.set_cookie(copy.copy(cookie))
    return session


@dataclass(frozen=True)
class Quality:
    value: int
    name: str
    nominal_kbps: int | None = None


@dataclass
class Stream:
    platform: str
    room: str
    requested: int
    actual: int
    actual_name: str
    qualities: list[Quality]
    urls: list[str] = field(repr=False)
    nominal_kbps: int | None = None
    backup_cdns: list[str] = field(default_factory=list, repr=False)
    codec: str = "AVC"

    def summary(self) -> dict:
        return {
            "platform": self.platform, "room": self.room,
            "requested": self.requested, "actual": self.actual,
            "actual_name": self.actual_name, "different_from_request": self.requested != self.actual,
            "nominal_kbps": self.nominal_kbps, "format": "HTTP-FLV", "codec_requested": self.codec,
            "cdn_candidates": len(self.urls) + len(self.backup_cdns),
        }


@dataclass(frozen=True)
class DouyuRoom:
    room: str
    live_status: int | None = None


def parse_douyu_room(page: str, requested_room: str) -> DouyuRoom:
    """Extract the main room's identity from HTML / Next.js data, never execute JS."""
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_script = False
            self.parts, self.scripts = [], []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.in_script, self.parts = True, []

        def handle_endtag(self, tag):
            if tag == "script" and self.in_script:
                self.scripts.append("".join(self.parts))
                self.in_script = False

        def handle_data(self, data):
            if self.in_script:
                self.parts.append(data)

    parser = Scripts()
    parser.feed(page)
    flight_parts = []
    for script in parser.scripts:
        match = re.fullmatch(r"\s*self\.__next_f\.push\((\[.*\])\)\s*;?\s*", script, re.S)
        if match:
            try:
                payload = json.loads(match[1])
                if len(payload) == 2 and payload[0] == 1 and isinstance(payload[1], str):
                    flight_parts.append(payload[1])
            except (ValueError, TypeError):
                continue
    # Flight chunks can split a single JSON object; concatenate before parsing.
    sources = ["".join(flight_parts)] + parser.scripts
    decoder = json.JSONDecoder()
    for source in sources:
        for match in re.finditer(r'"roomInfo"\s*:\s*(\{)', source):
            try:
                info, _ = decoder.raw_decode(source[match.start(1):])
                room = info["room"]
                actual = str(int(room["room_id"]))
                alias = next((room[k] for k in ("vipId", "vip_id", "short_id") if room.get(k)), None)
                if alias is not None and str(alias) != requested_room and actual != requested_room:
                    continue
                if not re.fullmatch(r"[1-9][0-9]{0,14}", actual):
                    continue
                status = int(room["show_status"]) if room.get("show_status") is not None else None
                return DouyuRoom(actual, status)
            except (ValueError, TypeError, KeyError):
                continue
    for script in parser.scripts:
        # Legacy pages expose the main room through a specific JS assignment.
        match = re.search(r"(?:^|[;\n])\s*(?:window\.room_id|\$ROOM\.room_id)\s*=\s*([1-9][0-9]{0,14})\s*;", script)
        if match:
            return DouyuRoom(match[1])
    raise ViewerError("斗鱼网页未提供可确认的真实房间号，请稍后重试。")


# 抖音直播档位：数值越大越清晰。ao 是纯音频流（无分辨率、fps 为 0），
# 交给播放器只会得到黑屏，因此不在此表中，解析时直接丢弃。
DOUYIN_TIERS = {"origin": (6, "原画"), "uhd": (5, "超清"), "hd": (4, "高清"),
                "sd": (3, "标清"), "ld": (2, "流畅"), "md": (1, "极速")}
# 旧结构 flv_pull_url 的键。仅在新结构缺失时使用。
DOUYIN_LEGACY_TIERS = {"FULL_HD1": (6, "原画"), "HD1": (4, "高清"),
                       "SD1": (3, "标清"), "SD2": (2, "流畅")}


def douyin_secure(url: str) -> str:
    """抖音接口下发的是 http 地址，但同一 CDN 主机同时提供 https。

    升级为 https 后即可沿用 validate_media_url 的 HTTPS-only 约束与证书校验，
    不需要为抖音放宽安全规则。
    """
    if not isinstance(url, str) or not url:
        return ""
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


def douyin_streams(stream_url: dict) -> list[dict]:
    """从抖音 stream_url 提取可选 FLV 档位，按清晰度降序。

    新结构 live_core_sdk_data 优先，旧结构 flv_pull_url 兜底。只保留带 FLV 地址的
    档位；优先 h264，只有该房间完全没有 h264 时才退回其他编码（PotPlayer 可解 HEVC）。
    """
    tiers: list[dict] = []
    core = (stream_url or {}).get("live_core_sdk_data") or {}
    raw = (core.get("pull_data") or {}).get("stream_data")
    if raw:
        try:
            decoded = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            decoded = {}
        for key, tier in ((decoded.get("data") or {}) if isinstance(decoded, dict) else {}).items():
            if key not in DOUYIN_TIERS:
                continue  # 丢弃 ao 等纯音频档位
            main = (tier or {}).get("main") or {}
            backup = (tier or {}).get("backup") or {}
            urls = [douyin_secure(main.get("flv")), douyin_secure(backup.get("flv"))]
            urls = [url for url in urls if url]
            if not urls:
                continue
            try:
                params = json.loads(main.get("sdk_params") or "{}")
            except (ValueError, TypeError):
                params = {}
            # 实测字段名是 VCodec / vbitrate（大小写敏感）。
            codec = str(params.get("VCodec") or "").lower()
            kbps = params.get("vbitrate")
            value, name = DOUYIN_TIERS[key]
            tiers.append({"value": value, "name": name, "urls": urls, "codec": codec,
                          "kbps": int(kbps) // 1000 if isinstance(kbps, (int, float)) and kbps else None,
                          "layout": "live_core_sdk_data"})
    if not tiers:
        for key, url in ((stream_url or {}).get("flv_pull_url") or {}).items():
            if key not in DOUYIN_LEGACY_TIERS or not url:
                continue
            value, name = DOUYIN_LEGACY_TIERS[key]
            tiers.append({"value": value, "name": name, "urls": [douyin_secure(url)], "codec": "",
                          "kbps": None, "layout": "flv_pull_url"})
    avc = [tier for tier in tiers if tier["codec"] in ("h264", "avc", "")]
    chosen = avc or tiers
    chosen.sort(key=lambda tier: tier["value"], reverse=True)
    return chosen


class PlatformClient:
    def __init__(self, platform: str, cookies=None, *, network_mode="direct"):
        spec = require_spec(platform)
        self.platform, self.spec = platform, spec
        self.session = new_session(cookies, network_mode=network_mode)
        self.session.headers["Referer"] = spec.referer
        device = spec.device_cookie or "dy_did"
        self.did = next((c.value for c in self.session.cookies if c.name == device), uuid.uuid4().hex)
        if spec.device_cookie:
            self.session.cookies.set(spec.device_cookie, self.did,
                                     domain="." + spec.cookie_domains[0], path="/")

    def close(self):
        self.session.close()

    def _api(self, url, *, params=None, data=None):
        try:
            response = self.session.request("POST" if data is not None else "GET", url,
                                            params=params, data=data, timeout=(8, 15))
            with response:
                if response.status_code != 200:
                    raise ViewerError(f"平台接口返回 HTTP {response.status_code}，请稍后重试。")
                return response.json(), response.headers
        except requests.RequestException as exc:
            raise ViewerError(connection_message(exc, "平台接口", url, self.session.viewer_network_mode),
                              code=connection_kind(exc)[1]) from None
        except ValueError:
            raise ViewerError("平台接口返回格式发生变化，请稍后重试。") from None

    def login_status(self) -> dict:
        if self.spec.verify_login:
            data, _ = self._api("https://api.bilibili.com/x/web-interface/nav")
            valid = data.get("code") == 0 and data.get("data", {}).get("isLogin") is True
            suffix = "有效" if valid else "无效或已过期"
            return {"verified": valid, "message": f"{self.spec.caption}登录状态{suffix}"}
        # 平台没有可用的身份校验接口时明确返回 None，界面据此不显示“已验证”。
        return {"verified": None, "message": self.spec.unverified_message}

    def resolve(self, room: str, requested: int | None = None) -> Stream:
        resolver = getattr(self, self.spec.resolve_method, None)
        if resolver is None:
            raise ViewerError("该平台的取流实现缺失。", code="platform.unimplemented")
        try:
            return resolver(room, self.spec.preferred_quality if requested is None else requested)
        except (KeyError, TypeError, IndexError, ValueError, OverflowError):
            raise ViewerError("平台返回的数据不完整或格式已变化。") from None

    def _bilibili(self, room, requested):
        init, _ = self._api("https://api.live.bilibili.com/room/v1/Room/room_init", params={"id": room})
        if init.get("code") != 0:
            raise ViewerError(f"B 站房间查询失败（代码 {init.get('code')}）。")
        info = init["data"]
        if info.get("live_status") != 1:
            raise ViewerError("该 B 站房间当前未开播（轮播不视为直播）。", code="room.offline", level="WARN")
        actual_room = str(info["room_id"])
        data, _ = self._api("https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo", params={
            "room_id": actual_room, "qn": requested, "protocol": "0", "format": "0", "codec": "0", "platform": "web",
        })
        if data.get("code") != 0:
            raise ViewerError(f"B 站播放接口返回代码 {data.get('code')}。")
        play = (data.get("data", {}).get("playurl_info") or {}).get("playurl", {})
        names = {int(q["qn"]): q["desc"] for q in play.get("g_qn_desc", [])}
        for protocol in play.get("stream", []):
            if protocol.get("protocol_name") != "http_stream":
                continue
            for fmt in protocol.get("format", []):
                if fmt.get("format_name") != "flv":
                    continue
                for codec in fmt.get("codec", []):
                    if codec.get("codec_name") != "avc":
                        continue
                    actual = int(codec["current_qn"])
                    ids = set(map(int, codec.get("accept_qn", []))) | {actual}
                    qualities = [Quality(q, names.get(q, str(q))) for q in sorted(ids, reverse=True)]
                    urls = [entry["host"] + codec["base_url"] + entry["extra"] for entry in codec.get("url_info", [])]
                    if urls:
                        return Stream("bilibili", actual_room, requested, actual, names.get(actual, str(actual)), qualities, urls)
        raise ViewerError("当前未返回可供本工具播放的 AVC FLV 流。")

    def _douyu_room(self, room):
        try:
            with self.session.get(f"https://www.douyu.com/{room}", timeout=(8, 15)) as response:
                if response.status_code != 200:
                    raise ViewerError(f"斗鱼房间网页返回 HTTP {response.status_code}。")
                return parse_douyu_room(response.text, room)
        except requests.RequestException as exc:
            raise ViewerError(connection_message(exc, "斗鱼房间查询", "https://www.douyu.com/", self.session.viewer_network_mode),
                              code=connection_kind(exc)[1]) from None

    def _douyu(self, room, requested):
        info = self._douyu_room(room)
        if info.live_status is not None and info.live_status != 1:
            raise ViewerError("该斗鱼房间当前未开播。", code="room.offline", level="WARN")
        room = info.room
        play = self._douyu_play(room, requested)
        qualities = [Quality(int(q["rate"]), q["name"], int(q.get("bit") or 0) or None) for q in play.get("multirates", [])]
        # Never infer the returned rate from the request or the advertised list.
        if "rate" not in play:
            raise ViewerError("斗鱼未返回实际 rate，无法可靠确认画质。")
        actual = int(play["rate"])
        match = next((q for q in qualities if q.value == actual), Quality(actual, f"档位 {actual}"))
        if not qualities:
            qualities = [match]
        url = play["rtmp_url"].rstrip("/") + "/" + play["rtmp_live"].lstrip("/")
        backups = []
        for entry in play.get("cdnsWithName") or []:
            cdn = entry.get("cdn") if isinstance(entry, dict) else None
            if (isinstance(cdn, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cdn)
                    and cdn != play.get("rtmp_cdn") and cdn not in backups):
                backups.append(cdn)
        # Prefer the verified Huawei route, and bound retry work on large lists.
        backups.sort(key=lambda cdn: cdn != "hw-h5")
        return Stream("douyu", str(room), requested, actual, match.name, qualities,
                      [url], match.nominal_kbps, backups[:3])

    def _douyu_play(self, room, requested, cdn=""):
        """Sign every request afresh, retaining this client's login cookies."""
        encrypted, headers = self._api("https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption", params={"did": self.did})
        if encrypted.get("error") != 0 or not encrypted.get("data"):
            raise ViewerError("斗鱼签名参数获取失败。")
        enc = encrypted["data"]
        try:
            stamp = int(parsedate_to_datetime(headers["date"]).timestamp())
        except (KeyError, ValueError, TypeError):
            raise ViewerError("斗鱼未返回有效服务器时间，请稍后重试。") from None
        rounds = int(enc["enc_time"])
        if not 0 <= rounds <= 10000:
            raise ViewerError("斗鱼签名参数异常。")
        digest = enc["rand_str"]
        for _ in range(rounds):
            digest = hashlib.md5((digest + enc["key"]).encode()).hexdigest()
        suffix = "" if enc["is_special"] == 1 else f"{room}{stamp}"
        auth = hashlib.md5((digest + enc["key"] + suffix).encode()).hexdigest()
        data, _ = self._api(f"https://www.douyu.com/lapi/live/getH5PlayV1/{room}", data={
            "enc_data": enc["enc_data"], "tt": str(stamp), "did": self.did, "auth": auth,
            "cdn": cdn, "rate": str(requested), "hevc": "0", "fa": "0", "ive": "0",
        })
        if data.get("error") != 0 or not data.get("data"):
            code = data.get("error")
            detail = "真实房间不可用或接口受限" if code == -4 else "可能未开播或接口受限"
            raise ViewerError(f"斗鱼无法取得直播流（代码 {code}，{detail}）。", code="platform.stream_rejected")
        play = data["data"]
        return play

    def _douyin_session(self):
        """未登录访问直播首页即可获得 ttwid；这是 enter 接口的唯一前提。"""
        if getattr(self, "douyin_ready", False):
            return
        try:
            with self.session.get("https://live.douyin.com/", timeout=(8, 20)) as response:
                if response.status_code != 200:
                    raise ViewerError(f"抖音首页返回 HTTP {response.status_code}，请稍后重试。")
        except requests.RequestException as exc:
            raise ViewerError(connection_message(exc, "抖音首页", "https://live.douyin.com/",
                                                 self.session.viewer_network_mode),
                              code=connection_kind(exc)[1]) from None
        if not any(cookie.name == "ttwid" and cookie.value for cookie in self.session.cookies):
            raise ViewerError("抖音未下发匿名会话标识（ttwid），请稍后重试或改用已登录 Cookie。",
                              code="platform.session_rejected")
        self.douyin_ready = True

    def _douyin_enter(self, room: str) -> dict:
        params = {
            "aid": "6383", "app_name": "douyin_web", "live_id": "1", "device_platform": "web",
            "language": "zh-CN", "enter_from": "web_live", "cookie_enabled": "true",
            "screen_width": "1920", "screen_height": "1080", "browser_language": "zh-CN",
            "browser_platform": "Win32", "browser_name": "Chrome", "browser_version": "140.0.0.0",
            "web_rid": room, "room_id_str": "", "enter_source": "", "is_need_double_stream": "false",
        }
        data, _ = self._api("https://live.douyin.com/webcast/room/web/enter/", params=params)
        code = data.get("status_code")
        if code == 0:
            return data
        if code == 4001038:
            raise ViewerError("抖音未接受该房间号，请确认直播间链接，或改用 12–15 位房间号。",
                              code="platform.stream_rejected", level="WARN")
        if code == 10011:
            raise ViewerError("抖音接口繁忙，请稍后重试。", code="platform.busy", level="WARN")
        raise ViewerError(f"抖音接口返回状态 {code}，请稍后重试。", code="platform.stream_rejected")

    def _douyin(self, room, requested):
        self._douyin_session()
        payload = self._douyin_enter(room)
        data = payload.get("data") or {}
        rooms = data.get("data") or []
        if not rooms:
            raise ViewerError("该抖音直播间不存在或当前未开播。", code="room.offline", level="WARN")
        info = rooms[0]
        if info.get("status") != 2:
            raise ViewerError("该抖音直播间当前未开播。", code="room.offline", level="WARN")
        streams = douyin_streams(info.get("stream_url") or {})
        if not streams:
            raise ViewerError("抖音未返回可供本工具播放的 FLV 流，该直播可能仅提供 HLS。",
                              code="platform.stream_rejected")
        # requested 是档位数值（6 = 原画）。取不高于请求值的最高档，全都更高时退回最低档。
        chosen = next((tier for tier in streams if tier["value"] <= requested), streams[-1])
        qualities = [Quality(tier["value"], tier["name"], tier["kbps"]) for tier in streams]
        codec = "AVC" if chosen["codec"] in ("h264", "avc", "") else "HEVC"
        return Stream("douyin", str(room), requested, chosen["value"], chosen["name"],
                      qualities, list(chosen["urls"]), chosen["kbps"], codec=codec)

    def media_urls(self, stream: Stream, *, on_event=None):
        """Resolve backup CDNs lazily, only after previous media opens fail."""
        yield from stream.urls
        if not self.spec.resign_backups:
            return
        seen = set(stream.urls)
        for cdn in stream.backup_cdns:
            try:
                play = self._douyu_play(stream.room, stream.actual, cdn)
                # A backup must not silently lower the stream's reported quality.
                if int(play["rate"]) != stream.actual:
                    if on_event:
                        on_event("relay.backup_quality_skipped", "备用线路画质与已确认档位不同，已跳过。", level="WARN")
                    continue
                url = play["rtmp_url"].rstrip("/") + "/" + play["rtmp_live"].lstrip("/")
            except (ViewerError, KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
                if on_event:
                    on_event("relay.backup_resolve_failed", str(exc) if isinstance(exc, ViewerError) else
                             "备用线路接口返回格式异常，已跳过。", level="WARN", impact="degraded")
                continue
            if url not in seen:
                seen.add(url)
                yield url


def validate_media_url(url: str, platform: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ViewerError("平台返回的直播地址不是受支持的 HTTPS 地址。")
    if not belongs_to(parsed.hostname or "", MEDIA_DOMAINS[platform]):
        host = parsed.hostname or ""
        # Expose only a bounded DNS name, never the signed path or query string.
        display = host if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,252}", host) else "无法识别"
        raise ViewerError(f"平台返回了尚未适配的 CDN 域名（{display}）。", code="platform.cdn_unsupported")


def open_media(session: requests.Session, url: str, platform: str):
    """Validate each redirect and leave the successful response open for streaming."""
    from urllib.parse import urljoin
    for _ in range(6):
        validate_media_url(url, platform)
        try:
            response = session.get(url, stream=True, allow_redirects=False, timeout=(8, 15),
                                   headers={"Referer": REFERERS[platform]})
        except requests.RequestException as exc:
            raise ViewerError(connection_message(exc, "CDN", url, getattr(session, "viewer_network_mode", "direct")),
                              code=connection_kind(exc)[1]) from None
        if response.status_code in (301, 302, 303, 307, 308):
            target = response.headers.get("Location", "")
            response.close()
            if not target:
                raise ViewerError("CDN 返回无效重定向。")
            url = urljoin(url, target)
            continue
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise ViewerError(f"CDN 返回 HTTP {status}，可尝试重新解析直播地址。")
        return response
    raise ViewerError("CDN 重定向次数过多。")


def read_flv_sample(client: PlatformClient, stream: Stream, seconds: float = 6, *, cancelled=None) -> dict:
    """Inspect a bounded sample; metadata is NOT proof of successful decoding."""
    result = {"verification": "container_only", "decoded": False}
    last_error = "CDN 连接或读取失败"
    def check_cancelled():
        if cancelled is not None and cancelled.is_set():
            raise ViewerError("操作已取消。", code="operation.cancelled", level="DEBUG")
    check_cancelled()
    for url in client.media_urls(stream):
        try:
            check_cancelled()
            with open_media(client.session, url, stream.platform) as response:
                raw = bytearray()
                started = time.monotonic()
                for chunk in response.iter_content(32768):
                    check_cancelled()
                    raw.extend(chunk)
                    if len(raw) >= 2 * 1024 * 1024 or time.monotonic() - started >= seconds:
                        break
            if raw[:3] != b"FLV":
                raise ViewerError("当前返回内容不是 FLV，暂不支持该流。")
            meta = {}
            for key in (b"width", b"height", b"framerate", b"videodatarate", b"audiodatarate"):
                marker = len(key).to_bytes(2, "big") + key + b"\x00"
                at = raw.find(marker)
                if at >= 0 and len(raw) >= at + len(marker) + 8:
                    number = struct.unpack(">d", raw[at + len(marker):at + len(marker) + 8])[0]
                    if 0 <= number < 10000000:
                        meta[key.decode()] = number
            cursor = int.from_bytes(raw[5:9], "big") + 4
            stamps, payload_bytes, video_tags, audio_tags = [], 0, 0, 0
            video_codec = None
            while cursor + 11 <= len(raw):
                kind = raw[cursor]
                length = int.from_bytes(raw[cursor + 1:cursor + 4], "big")
                end = cursor + 11 + length
                if end + 4 > len(raw):
                    break
                stamp = int.from_bytes(raw[cursor + 4:cursor + 7], "big") + (raw[cursor + 7] << 24)
                if kind == 9:
                    video_tags += 1
                    if length:
                        video_codec = {7: "H.264", 12: "HEVC"}.get(raw[cursor + 11] & 15, "未识别")
                    if length > 1 and raw[cursor + 12] == 1:
                        stamps.append(stamp)
                        payload_bytes += length
                elif kind == 8:
                    audio_tags += 1
                    if length > 1 and raw[cursor + 12] == 1:
                        payload_bytes += length
                cursor = end + 4
            span = (max(stamps) - min(stamps)) / 1000 if stamps else 0
            result.update({"metadata": meta, "video_codec": video_codec, "video_tags": video_tags,
                           "audio_tags": audio_tags, "bytes_read": len(raw), "media_seconds": round(span, 2),
                           "sample_total_kbps": round(payload_bytes * 8 / span / 1000) if span > 0 else None})
            return result
        except ViewerError as exc:
            if exc.code == "operation.cancelled":
                raise
            last_error = str(exc)
            continue
        except requests.RequestException:
            last_error = "CDN 连接或读取超时"
            continue
    raise ViewerError("短时流读取失败：" + last_error)


def comparison(platform: str, room: str, requested: int | None, cookies, *, network_mode="direct") -> tuple[list[dict], list[Quality]]:
    rows, available = [], {}
    for label, jar in (("匿名", None), ("已导入 Cookie", cookies)):
        if label != "匿名" and cookies is None:
            rows.append({"session": label, "status": "未导入 Cookie，登录对比尚未执行"})
            continue
        client = PlatformClient(platform, jar, network_mode=network_mode)
        row = {"session": label, "tested_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        try:
            if jar is not None:
                login = client.login_status()
                row["login"] = login
                if login["verified"] is False:
                    raise ViewerError(login["message"])
            resolved = client.resolve(room, requested)
            row.update(resolved.summary())
            available.update({q.value: q for q in resolved.qualities})
            try:
                row["sample"] = read_flv_sample(client, resolved)
                row["status"] = "流读取成功；解码与实播待验证"
            except ViewerError as exc:
                row["status"] = str(exc)
                row["error_code"], row["level"] = exc.code, exc.level
        except ViewerError as exc:
            row["status"] = str(exc)
            row["error_code"], row["level"] = exc.code, exc.level
        finally:
            client.close()
        rows.append(row)
    return rows, list(available.values())


def save_report(path: str | Path, rows: list[dict], playback: dict):
    # The report consists solely of safe summaries; no Stream, URL, headers or cookie jar.
    Path(path).write_text(json.dumps({"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                    "comparison": rows, "playback": playback}, ensure_ascii=False, indent=2), encoding="utf-8")
