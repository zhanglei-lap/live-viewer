"""PGC validation adapter. Episode authorization is separate from VIP status."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from urllib.parse import urlparse

import requests

from .core import ViewerError, new_session
from .network import connection_message
from .vod import Track, Video, VideoSource, media_url, wbi_params
from .vod_proxy import VodProxy


def bangumi_input(value):
    value = str(value).strip()
    if value.startswith(("https://", "http://")):
        try:
            parsed = urlparse(value)
            if (parsed.hostname not in ("www.bilibili.com", "bilibili.com", "m.bilibili.com")
                    or parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
                raise ValueError()
            value = parsed.path.removeprefix("/bangumi/play/").rstrip("/")
        except ValueError:
            value = ""
    # Bare digits are explicitly EP IDs, never guessed to be a season or AV.
    if re.fullmatch(r"[1-9][0-9]{0,15}", value):
        value = "ep" + value
    match = re.fullmatch(r"(ep|ss)([1-9][0-9]{0,15})", value, re.I)
    if not match:
        raise ViewerError("请输入 EP / SS 编号或 B 站 /bangumi/play/ 链接。", code="pgc.input_invalid", level="WARN")
    return {"ep_id" if match[1].lower() == "ep" else "season_id": int(match[2])}


def positive(value, default=0):
    return value if type(value) is int and 0 < value < 10**16 else default


def seconds(value, scale=1):
    try:
        result = float(value) / scale
        return result if math.isfinite(result) and 0 < result < 31536000 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class Episode:
    ep_id: int
    season_id: int
    cid: int
    aid: int
    bvid: str
    number: str
    title: str
    duration: float
    section: str = "正片"
    status: int = 0
    badge: str = ""

    def summary(self):
        return dict(self.__dict__)


@dataclass(frozen=True)
class Season:
    season_id: int
    title: str
    episodes: tuple[Episode, ...]
    seasons: tuple[dict, ...] = ()

    def episode(self, ep_id):
        found = next((ep for ep in self.episodes if ep.ep_id == ep_id), None)
        if found is None:
            raise ViewerError("季度信息中没有目标 EP，已停止解析。", code="pgc.episode_missing", level="WARN")
        return found


@dataclass
class BangumiSource(VideoSource):
    episode: Episode | None = None
    season_title: str = ""
    account: dict = field(default_factory=dict)
    media_duration: float = 0
    play_detail: str = ""

    @property
    def referer(self):
        return f"https://www.bilibili.com/bangumi/play/ep{self.episode.ep_id}"

    def summary(self):
        return {**super().summary(), "content_type": "pgc", "episode": self.episode.summary(),
                "season_title": self.season_title, "account": self.account,
                "media_duration_seconds": self.media_duration, "play_detail": self.play_detail}


@dataclass
class Inspection:
    season: Season
    episode: Episode
    account: dict
    status: str
    message: str
    evidence: dict
    source: BangumiSource | None = field(default=None, repr=False)

    def summary(self):
        return {"season_title": self.season.title, "episode": self.episode.summary(),
                "account": self.account, "status": self.status, "message": self.message,
                "evidence": self.evidence, "media": self.source.summary() if self.source else None,
                "available_video_tracks": [t.summary() for t in self.source.videos] if self.source else [],
                "available_audio_tracks": [t.summary() for t in self.source.audios] if self.source else []}


ERRORS = {
    -101: ("account.expired", "B 站登录已失效，请重新扫码登录。"),
    -404: ("pgc.not_found", "番剧或剧集不存在、已删除或暂不可见。"),
    -352: ("pgc.rate_limited", "平台暂时限制番剧接口，请稍后重试。"),
    -403: ("pgc.access_denied", "平台拒绝本次番剧请求。"),
    -10403: ("pgc.entitlement_required", "当前会话没有整集观看资格。"),
}


def api_error(code, provider_message=""):
    # -10403 is also used for regional restrictions; the number alone does
    # not establish a VIP-only reason. Inspect the signal, never export raw text.
    if type(code) is not int:
        return ViewerError("番剧接口缺少有效的结果代码。", code="pgc.response_invalid")
    if code == -10403 and re.search(r"地区|区域|region|area", str(provider_message)[:500], re.I):
        return ViewerError("平台明确返回地区观看限制。", code="pgc.region_restricted", level="WARN")
    error, message = ERRORS.get(code, ("pgc.api_rejected", "番剧接口暂不可用。"))
    return ViewerError(f"{message}（接口代码 {code if type(code) is int else '未知'}）", code=error, level="WARN")


def play_payload(raw):
    """Normalize documented v2 envelopes, preserving business permission fields."""
    if not isinstance(raw, dict):
        raise ViewerError("番剧接口返回了无法识别的数据。", code="pgc.response_invalid")
    if raw.get("code") != 0:
        raise api_error(raw.get("code"), raw.get("message", ""))
    for container in (raw.get("result"), raw.get("data")):
        if not isinstance(container, dict):
            continue
        if "result" in container and isinstance(container["result"], dict):
            if container.get("code", 0) != 0:
                raise api_error(container["code"], container.get("message", ""))
            container = container["result"]
        if isinstance(container.get("video_info"), dict):
            return container, container["video_info"]
    raise ViewerError("番剧播放接口缺少 video_info。", code="pgc.response_invalid")


def dash_tracks(raw, kind):
    names = {q["quality"]: str(q.get("new_description") or q.get("description") or q["quality"])[:80]
             for q in raw.get("support_formats", []) if isinstance(q, dict) and type(q.get("quality")) is int}
    dash = raw.get("dash") or {}
    values = []
    for item in (dash.get(kind) or [])[:100]:
        if not isinstance(item, dict):
            continue
        codec = str(item.get("codecs", ""))[:100]
        quality = positive(item.get("id"))
        if (not codec.startswith(("avc1", "hev1", "hvc1", "av01") if kind == "video" else ("mp4a",))
                or kind == "video" and quality in (125, 126)):
            continue
        urls = []
        for url in [item.get("baseUrl") or item.get("base_url"),
                    *(item.get("backupUrl") or item.get("backup_url") or [])[:8]]:
            if not isinstance(url, str):
                continue
            try:
                urls.append(media_url(url))
            except (ViewerError, ValueError):
                continue  # An unknown backup cannot invalidate a supported primary.
        if not urls:
            continue
        rate = positive(item.get("bandwidth"))
        width, height = positive(item.get("width")), positive(item.get("height"))
        encoding = "AVC" if codec.startswith("avc1") else "HEVC" if codec.startswith(("hev1", "hvc1")) else "AV1"
        label = f"{names.get(quality, str(quality))} · {width}×{height} · {encoding}" if kind == "video" else f"AAC · {rate/1000:.0f} kbps"
        values.append(Track(f"{kind}:{quality}:{codec}:{rate}", kind, quality, label, codec, rate,
                            tuple(dict.fromkeys(urls)), width, height, str(item.get("frameRate", item.get("frame_rate", "")))[:30]))
    return tuple(sorted({t.key: t for t in values}.values(), key=lambda t: (t.quality, t.codec.startswith("avc1"), t.bandwidth), reverse=True))


class BangumiClient:
    def __init__(self, cookies=None, *, network_mode="direct"):
        self.login_requested = bool(cookies)
        self.session = new_session(cookies, network_mode=network_mode)
        self.session.headers["Referer"] = "https://www.bilibili.com/"

    def close(self):
        self.session.close()

    def json(self, endpoint, params=None):
        try:
            with self.session.get("https://api.bilibili.com" + endpoint, params=params, timeout=(5, 15)) as response:
                response.raise_for_status()
                raw = response.json()
        except requests.exceptions.JSONDecodeError:
            raise ViewerError("番剧接口返回了无法识别的数据。", code="pgc.response_invalid") from None
        except requests.RequestException as exc:
            raise ViewerError(connection_message(exc, "api.bilibili.com", self.session.viewer_network_mode), code="pgc.network_failed") from None
        if not isinstance(raw, dict):
            raise ViewerError("番剧接口返回了无法识别的数据。", code="pgc.response_invalid")
        return raw

    def account_state(self):
        raw = self.json("/x/web-interface/nav")
        if raw.get("code") not in (0, -101):
            raise api_error(raw.get("code"), raw.get("message", ""))
        nav = raw.get("data") or {}
        login = nav.get("isLogin") is True
        if self.login_requested and not login:
            raise api_error(-101)
        vip = nav.get("vip") or {}
        # No UID, display name, birthday or session material enters the report.
        return {"login": "authenticated" if login else "anonymous",
                "vip": "active" if login and vip.get("status") == 1 else "inactive",
                "network_mode": self.session.viewer_network_mode, "trust_env": self.session.trust_env}

    def metadata(self, value):
        params = bangumi_input(value)
        raw = self.json("/pgc/view/web/season", params)
        if raw.get("code") != 0:
            raise api_error(raw.get("code"), raw.get("message", ""))
        data = raw.get("result") or raw.get("data") or {}
        sid = positive(data.get("season_id"))
        if not sid or "season_id" in params and sid != params["season_id"]:
            raise ViewerError("番剧季度标识不匹配。", code="pgc.identity_mismatch")
        items = [("正片", ep) for ep in data.get("episodes", [])[:1000]]
        for section in data.get("section", [])[:100]:
            items += [(str(section.get("title", "附加内容"))[:100], ep) for ep in section.get("episodes", [])[:1000]]
        episodes = {}
        for section, ep in items:
            if not isinstance(ep, dict) or not positive(ep.get("id")):
                continue
            bvid = str(ep.get("bvid", ""))
            episodes.setdefault(ep["id"], Episode(ep["id"], sid, positive(ep.get("cid")), positive(ep.get("aid")),
                bvid if re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) else "", str(ep.get("title", ""))[:80],
                str(ep.get("long_title", ""))[:300], seconds(ep.get("duration"), 1000), section,
                positive(ep.get("status")), str(ep.get("badge", ""))[:40]))
        related = {sid: {"season_id": sid, "title": str(data.get("season_title") or data.get("title", ""))[:300]}}
        for item in data.get("seasons", [])[:100]:
            if isinstance(item, dict) and positive(item.get("season_id")):
                related[item["season_id"]] = {"season_id": item["season_id"], "title": str(item.get("season_title") or item.get("title") or item["season_id"])[:300]}
        season = Season(sid, str(data.get("title", ""))[:300], tuple(episodes.values()), tuple(related.values()))
        if "ep_id" in params:
            season.episode(params["ep_id"])
        return season

    def inspect(self, value, *, ep_id=None, video_key=None, audio_key=None, expected_cid=None, requested=127):
        params = bangumi_input(value)
        season = self.metadata(value)
        target = ep_id or params.get("ep_id")
        if target is None:
            target = next((e.ep_id for e in season.episodes if e.section == "正片"), None)
        episode = season.episode(target)
        if not episode.cid and not episode.duration:
            raise ViewerError("目标剧集尚未提供可播放媒体，可能尚未上线。", code="pgc.not_published", level="WARN")
        if not episode.cid or expected_cid is not None and expected_cid != episode.cid:
            raise ViewerError("目标 EP 的 cid 无效或已经改变，已停止恢复。", code="pgc.identity_mismatch")
        account = self.account_state()
        self.session.headers["Referer"] = f"https://www.bilibili.com/bangumi/play/ep{episode.ep_id}"
        container, raw = play_payload(self.json("/pgc/player/web/v2/playurl",
            {"ep_id": episode.ep_id, "cid": episode.cid, "qn": requested, "fnval": 4048, "fnver": 0, "fourk": 1}))
        business = container.get("play_view_business_info") or {}
        pgc_user = business.get("user_status") or {}
        pgc_login = pgc_user.get("is_login")
        real_vip = (pgc_user.get("vip_info") or {}).get("real_vip")
        account["pgc_login"] = "authenticated" if pgc_login in (1, True) else "anonymous" if pgc_login in (0, False) else "not_provided"
        account["pgc_vip"] = "active" if real_vip is True else "inactive" if real_vip is False else "not_provided"
        returned = business.get("episode_info") or {}
        for key, expected in (("ep_id", episode.ep_id), ("cid", episode.cid)):
            if key in returned and returned[key] != expected:
                raise ViewerError("播放接口返回了另一集，已停止播放。", code="pgc.identity_mismatch")
        returned_season = (business.get("season_info") or {}).get("season_id")
        if returned_season is not None and returned_season != episode.season_id:
            raise ViewerError("播放接口返回的季度不匹配。", code="pgc.identity_mismatch")
        detail = (container.get("play_check") or {}).get("play_detail", "")
        if detail not in ("", "PLAY_WHOLE", "PLAY_PREVIEW", "PLAY_NONE"):
            detail = "unknown"
        duration = seconds((raw.get("dash") or {}).get("duration"))
        if not duration and isinstance(raw.get("durl"), list):
            duration = sum(seconds(x.get("length"), 1000) for x in raw["durl"] if isinstance(x, dict))
        advertised = [{"quality": positive(f.get("quality")), "label": str(f.get("new_description", ""))[:80],
                       "need_vip": bool(f.get("need_vip")), "need_login": bool(f.get("need_login"))}
                      for f in raw.get("support_formats", [])[:30] if isinstance(f, dict)]
        evidence = {"requested": requested, "returned_quality": positive(raw.get("quality")), "play_detail": detail,
                    "is_preview": bool(raw.get("is_preview")), "is_drm": bool(raw.get("is_drm")),
                    "api_total_seconds": seconds(raw.get("timelength"), 1000), "media_seconds": duration,
                    "advertised_formats": advertised, "video_error_code": raw.get("error_code") if type(raw.get("error_code")) is int else None}
        evidence["rights_hint"] = "paid" if re.search(r"付费|购买", episode.badge) else "vip" if episode.status == 13 or "会员" in episode.badge else "not_provided"
        def result(status, message, source=None):
            return Inspection(season, episode, account, status, message, evidence, source)
        plugins = (container.get("view_info") or {}).get("plugins") or {}
        if (plugins.get("AreaLimitPanel") or {}).get("config", {}).get("is_block"):
            return result("pgc.region_restricted", "平台明确返回地区观看限制。")
        if raw.get("is_preview") or raw.get("is_trial") or detail == "PLAY_PREVIEW" or container.get("play_video_type") == "preview":
            return result("pgc.preview_only", "当前会话仅获试看，不能按整集播放。")
        code = raw.get("error_code") or raw.get("code") or 0
        if code or detail == "PLAY_NONE" or container.get("play_video_type") == "none":
            error = api_error(code, raw.get("message", "")) if code else ViewerError("平台未授权当前集播放。", code="pgc.entitlement_required")
            return result(error.code, str(error))
        if raw.get("is_drm") or raw.get("drm_tech_type") or raw.get("drm_type"):
            return result("pgc.protected_media", "平台返回受保护媒体，当前普通 DASH 链路不能直接播放。")
        if not duration or not episode.duration or abs(duration - episode.duration) > max(5, episode.duration * .02):
            return result("pgc.duration_mismatch", "媒体实际时长与整集元数据不一致，不能确认整集。")
        videos, audios = dash_tracks(raw, "video"), dash_tracks(raw, "audio")
        if not videos or not audios:
            return result("pgc.tracks_unavailable", "没有可直接播放的 SDR DASH 视频与 AAC 音轨。")
        def choose(tracks, key):
            # PGC can return every authorized track even when qn=80. Selection,
            # rather than qn alone, determines what the native player decodes.
            if not key:
                choices = [t for t in tracks if t.kind != "video" or t.quality <= requested]
                chosen = choices[0] if choices else None
            else:
                chosen = next((t for t in tracks if t.key == key), None)
            if chosen is None:
                raise ViewerError("原番剧轨道已不可用，已停止恢复。", code="pgc.track_missing", level="WARN")
            return chosen
        page = {"page": 1, "cid": episode.cid, "title": episode.title, "duration": episode.duration}
        source = BangumiSource(Video(episode.bvid, episode.aid, season.title + " · " + episode.number + " " + episode.title, (page,)),
            page, videos, audios, choose(videos, video_key), choose(audios, audio_key), requested,
            episode=episode, season_title=season.title, account=account, media_duration=duration, play_detail=detail)
        return result("pgc.whole", "平台返回完整普通 DASH，可进行实际播放验证。", source)

    def resolve(self, value, **kwargs):
        inspected = self.inspect(value, **kwargs)
        if inspected.source is None:
            raise ViewerError(inspected.message, code=inspected.status, level="WARN")
        return inspected.source

    def subtitle_catalog(self, episode):
        from .subtitles import catalog
        self.account_state()
        nav = self.json("/x/web-interface/nav").get("data") or {}
        images = nav.get("wbi_img") or {}
        lookup = "".join(urlparse(images.get(k, "")).path.rsplit("/", 1)[-1].split(".")[0] for k in ("img_url", "sub_url"))
        params = {"cid": episode.cid, "ep_id": episode.ep_id, "season_id": episode.season_id}
        if episode.aid:params["aid"] = episode.aid
        elif episode.bvid:params["bvid"] = episode.bvid
        else:raise ViewerError("当前集没有可用于查询平台 CC 字幕的标识。", code="pgc.subtitle_unavailable", level="WARN")
        self.session.headers["Referer"] = f"https://www.bilibili.com/bangumi/play/ep{episode.ep_id}"
        raw = self.json("/x/player/wbi/v2", wbi_params(params, lookup))
        if raw.get("code") != 0:raise api_error(raw.get("code"), raw.get("message", ""))
        return catalog(raw.get("data") or {})


class BangumiProxy(VodProxy):
    """Reuse P7 Range transport without changing its UGC behavior or data model."""
    def __init__(self, source, *, refresher=None, **kwargs):
        def pinned(old):
            fresh = refresher(old)
            if (fresh.episode.ep_id, fresh.episode.season_id, fresh.episode.cid) != (
                    old.episode.ep_id, old.episode.season_id, old.episode.cid):
                raise ViewerError("续期后的 EP / cid / 季度不匹配。", code="pgc.identity_mismatch")
            return fresh
        super().__init__(source, refresher=pinned if refresher else None, **kwargs)

    def _session(self):
        session = new_session(network_mode=self.network_mode)
        session.headers["Referer"] = self.source_info.referer
        return session
