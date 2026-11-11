"""Ordinary Bilibili uploads: metadata, WBI play information and safe track selection."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from .core import ViewerError, belongs_to, new_session, UA
from .network import connection_message

MIXIN = (46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,
         14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,
         59,6,63,57,62,11,36,20,34,44,52)


def video_input(value):
    value = value.strip()
    page = 1
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if parsed.hostname not in ("www.bilibili.com", "bilibili.com", "m.bilibili.com") or parsed.username:
            raise ViewerError("请输入 B 站普通视频 BV / AV 编号或完整视频链接。", code="vod.input_invalid", level="WARN")
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,19})/?", parsed.path, re.I)
        if not match:
            raise ViewerError("目前支持普通投稿的 /video/BV 或 /video/av 链接。", code="vod.unsupported", level="WARN")
        value = match[1]
        try:
            page = int(parse_qs(parsed.query).get("p", ["1"])[0])
        except ValueError:
            page = 0
    if not 1 <= page <= 10000:
        raise ViewerError("分 P 编号无效。", code="vod.input_invalid", level="WARN")
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", value):
        return {"bvid": value}, page
    if re.fullmatch(r"av[1-9][0-9]{0,19}", value, re.I):
        return {"aid": int(value[2:])}, page
    raise ViewerError("请输入 BV 编号、av 编号或 B 站普通视频链接。", code="vod.input_invalid", level="WARN")


def media_url(url):
    parsed = urlparse(url)
    if (parsed.scheme not in ("http", "https") or parsed.username or parsed.password or
            parsed.port not in (None, 80, 443) or parsed.fragment or
            not belongs_to(parsed.hostname or "", ("bilivideo.com", "bilivideo.cn"))):
        raise ViewerError("视频接口返回了尚未适配的 CDN 地址。", code="vod.cdn_unsupported")
    # All supported CDN hosts must pass normal HTTPS certificate validation.
    return urlunparse(parsed._replace(scheme="https", netloc=parsed.hostname))


def wbi_params(params, lookup, now=None):
    if len(lookup) != 64 or not re.fullmatch("[0-9a-fA-F]{64}", lookup):
        raise ViewerError("无法取得播放接口签名信息，请稍后重试。", code="vod.sign_unavailable")
    key = "".join(lookup[i] for i in MIXIN)[:32]
    values = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted({**params, "wts": round(time.time() if now is None else now)}.items())}
    values["w_rid"] = hashlib.md5((urlencode(values) + key).encode()).hexdigest()
    return values


@dataclass(frozen=True)
class Video:
    bvid: str
    aid: int
    title: str
    pages: tuple[dict, ...]
    owner: str = ""

    def page(self, number):
        selected = next((p for p in self.pages if p["page"] == number), None)
        if selected is None:
            raise ViewerError("该视频没有所选分 P，请重新获取视频信息。", code="vod.page_missing", level="WARN")
        return selected


@dataclass(frozen=True)
class Track:
    key: str
    kind: str
    quality: int
    label: str
    codec: str
    bandwidth: int
    urls: tuple[str, ...] = field(repr=False)
    width: int = 0
    height: int = 0
    fps: str = ""

    def summary(self):
        return {k: getattr(self, k) for k in ("key", "kind", "quality", "label", "codec", "bandwidth", "width", "height", "fps")}


@dataclass
class VideoSource:
    video: Video
    page: dict
    videos: tuple[Track, ...]
    audios: tuple[Track, ...]
    selected_video: Track
    selected_audio: Track
    requested: int = 127
    subtitle: object | None = None
    subtitle_text: str | None = field(default=None, repr=False)

    @property
    def actual_name(self):
        return self.selected_video.label + " / " + self.selected_audio.label

    def summary(self):
        return {"kind": "vod", "platform": "bilibili", "bvid": self.video.bvid, "cid": self.page["cid"],
                "page": self.page["page"], "title": self.video.title, "duration_seconds": self.page["duration"],
                "format": "DASH 双轨", "requested": self.requested, "actual": self.selected_video.quality,
                "actual_name": self.actual_name, "video": self.selected_video.summary(), "audio": self.selected_audio.summary(),
                "subtitle": self.subtitle.summary() if self.subtitle else None}


class VodClient:
    def __init__(self, cookies=None, *, network_mode="direct"):
        self.login_requested = bool(cookies)
        self.session = new_session(cookies, network_mode=network_mode)
        self.session.headers["Referer"] = "https://www.bilibili.com/"

    def close(self):
        self.session.close()

    def json(self, endpoint, params=None, *, allow_anonymous_nav=False):
        try:
            response = self.session.get("https://api.bilibili.com" + endpoint, params=params, timeout=(5, 15))
            with response:
                response.raise_for_status()
                result = response.json()
        except requests.exceptions.JSONDecodeError:
            raise ViewerError("视频接口返回了无法识别的数据。", code="vod.response_invalid") from None
        except requests.RequestException as exc:
            raise ViewerError(connection_message(exc, "api.bilibili.com", self.session.viewer_network_mode), code="vod.network_failed") from None
        except ValueError:
            raise ViewerError("视频接口返回了无法识别的数据。", code="vod.response_invalid") from None
        if not isinstance(result, dict):
            raise ViewerError("视频接口返回了无法识别的数据。", code="vod.response_invalid")
        code = result.get("code")
        if code != 0 and not (allow_anonymous_nav and code == -101):
            message = {-404: "视频不存在或已删除。", -403: "当前账号或地区无法访问该视频。",
                       -101: "登录已失效，请重新登录。", -352: "平台暂时限制视频接口，请稍后重试。"}.get(code, "视频接口暂不可用。")
            error_code = {-404: "vod.not_found", -403: "vod.access_denied", -101: "account.expired", -352: "vod.rate_limited"}.get(code, "vod.api_rejected")
            raise ViewerError(f"{message}（接口代码 {code}）", code=error_code, level="WARN")
        if not isinstance(result.get("data"), dict):
            raise ViewerError("视频接口缺少有效数据。", code="vod.response_invalid")
        return result["data"]

    def metadata(self, value):
        params, number = video_input(value)
        raw = self.json("/x/web-interface/view", params)
        bvid = raw.get("bvid", "")
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or raw.get("rights", {}).get("is_stein_gate"):
            raise ViewerError("此内容类型暂未支持；当前支持普通投稿。", code="vod.unsupported", level="WARN")
        pages = tuple({"page": int(p["page"]), "cid": int(p["cid"]), "title": str(p.get("part", ""))[:300],
                       "duration": max(0, int(p.get("duration", 0)))} for p in raw.get("pages", [])[:10000]
                      if isinstance(p, dict) and type(p.get("page")) is int and p["page"] > 0 and type(p.get("cid")) is int and p["cid"] > 0)
        video = Video(bvid, int(raw["aid"]), str(raw.get("title", bvid))[:300], pages, str(raw.get("owner", {}).get("name", ""))[:80])
        video.page(number)
        return video, number

    def resolve(self, value, page=None, video_key=None, audio_key=None, *, cid=None):
        video, inferred = self.metadata(value)
        part = next((p for p in video.pages if p["cid"] == cid), None) if cid is not None else video.page(inferred if page is None else page)
        if part is None:raise ViewerError("原分 P 内容已变更或删除，已停止恢复。", code="vod.part_changed", level="WARN")
        nav = self.json("/x/web-interface/nav", allow_anonymous_nav=True)
        if self.login_requested and nav.get("isLogin") is not True:
            raise ViewerError("B 站登录已失效，请重新登录或选择匿名。", code="account.expired", level="WARN")
        images = nav.get("wbi_img", {})
        lookup = "".join(urlparse(images.get(k, "")).path.rsplit("/", 1)[-1].split(".")[0] for k in ("img_url", "sub_url"))
        self.signing_lookup = lookup
        params = wbi_params({"bvid": video.bvid, "cid": part["cid"], "qn": 127, "fnval": 4048, "fnver": 0, "fourk": 1}, lookup)
        raw = self.json("/x/player/wbi/playurl", params)
        if raw.get("is_preview") or raw.get("is_trial"):
            raise ViewerError("平台仅返回试看片段，此版本不将试看片段作为完整视频播放。", code="vod.preview_only", level="WARN")
        names = {int(q["quality"]): str(q.get("new_description") or q.get("display_desc") or q["quality"])
                 for q in raw.get("support_formats", []) if isinstance(q, dict) and type(q.get("quality")) is int}
        dash = raw.get("dash") or {}
        def tracks(kind):
            values = []
            for item in dash.get(kind, []) or []:
                codec = str(item.get("codecs", ""))
                if not codec.startswith(("avc1", "hev1", "hvc1", "av01") if kind == "video" else ("mp4a",)) or kind == "video" and item.get("id") in (125, 126):
                    continue
                urls = tuple(dict.fromkeys(media_url(u) for u in [item.get("baseUrl") or item.get("base_url"),
                    *(item.get("backupUrl") or item.get("backup_url") or [])] if isinstance(u, str)))
                if not urls:
                    continue
                quality, rate = int(item.get("id", 0)), max(0, int(item.get("bandwidth", 0)))
                key = f"{kind}:{quality}:{codec}:{rate}"
                encoding = "AVC" if codec.startswith("avc1") else "HEVC" if codec.startswith(("hev1", "hvc1")) else "AV1"
                label = (names.get(quality, f"画质 {quality}") + f" · {item.get('width', 0)}×{item.get('height', 0)} · {encoding}") if kind == "video" else f"AAC · {rate / 1000:.0f} kbps"
                values.append(Track(key, kind, quality, label, codec, rate, urls, int(item.get("width", 0)), int(item.get("height", 0)), str(item.get("frameRate", ""))))
            return tuple(sorted({t.key: t for t in values}.values(), key=lambda t: (t.quality, t.codec.startswith("avc1"), t.bandwidth), reverse=True))
        videos, audios = tracks("video"), tracks("audio")
        if not videos or not audios:
            raise ViewerError("当前会话没有可用的标准视频与 AAC 音轨，请登录或改用其他普通视频。", code="vod.tracks_unavailable", level="WARN")
        def choose(values, key):
            if not key:
                return values[0]
            found = next((t for t in values if t.key == key), None)
            if found is None:
                raise ViewerError("所选轨道已不可用，请重新获取可用画质。", code="vod.track_missing", level="WARN")
            return found
        selected_video = choose(videos, video_key)
        return VideoSource(video, part, videos, audios, selected_video, choose(audios, audio_key), selected_video.quality if video_key else 127)

    def subtitle_catalog(self, bvid, cid):
        from .subtitles import catalog
        lookup = getattr(self, "signing_lookup", None)
        if lookup is None:
            nav = self.json("/x/web-interface/nav", allow_anonymous_nav=True)
            if self.login_requested and nav.get("isLogin") is not True:
                raise ViewerError("B 站登录已失效，请重新登录。", code="account.expired", level="WARN")
            images = nav.get("wbi_img", {})
            lookup = "".join(urlparse(images.get(k, "")).path.rsplit("/", 1)[-1].split(".")[0] for k in ("img_url", "sub_url"))
        return catalog(self.json("/x/player/wbi/v2", wbi_params({"bvid": bvid, "cid": cid}, lookup)))
