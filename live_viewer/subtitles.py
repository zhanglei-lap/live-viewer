"""Platform subtitle catalog and bounded JSON-to-SRT conversion."""
from dataclasses import dataclass, field
import math
from urllib.parse import urljoin, urlparse, urlunparse
import requests
from .core import ViewerError, belongs_to, new_session


def subtitle_url(value):
    parsed = urlparse(urljoin("https://www.bilibili.com/", value))
    if (parsed.scheme not in ("https", "http") or parsed.username or parsed.password or parsed.fragment or
            parsed.port not in (None, 80, 443) or not belongs_to(parsed.hostname or "", ("hdslb.com", "bilibili.com")) or
            not parsed.path.startswith("/bfs/subtitle/") or not parsed.path.endswith(".json")):
        raise ViewerError("平台返回了尚未支持的字幕地址。", code="vod.subtitle_host_unsupported", level="WARN")
    return urlunparse(parsed._replace(scheme="https", netloc=parsed.hostname))


@dataclass(frozen=True)
class Subtitle:
    key: str
    language: str
    name: str
    url: str = field(repr=False)
    def summary(self):return {"key": self.key, "language": self.language, "name": self.name}


def catalog(data):
    result = []
    for raw in data.get("subtitle", {}).get("subtitles", [])[:100]:
        if not isinstance(raw, dict):continue
        language = raw.get("lan", "")
        identifier = str(raw.get("id_str") or raw.get("id") or "")
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", language) or not re.fullmatch(r"[0-9]{1,40}", identifier):continue
        result.append(Subtitle(identifier + ":" + language, language, str(raw.get("lan_doc") or language)[:80], subtitle_url(raw.get("subtitle_url", ""))))
    return tuple(result), bool(data.get("need_login_subtitle"))


def to_srt(data):
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, list) or len(body) > 20000:
        raise ViewerError("字幕数据格式或大小不受支持。", code="vod.subtitle_invalid", level="WARN")
    def stamp(value):
        milliseconds = round(value * 1000)
        seconds, milli = divmod(milliseconds, 1000);minutes, second = divmod(seconds, 60);hours, minute = divmod(minutes, 60)
        return f"{hours:02}:{minute:02}:{second:02},{milli:03}"
    lines = []
    for line in body:
        if not isinstance(line, dict):raise ViewerError("字幕条目无效。", code="vod.subtitle_invalid", level="WARN")
        start, end, content = line.get("from"), line.get("to"), line.get("content")
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)) or
                not 0 <= start < end <= 31536000 or not isinstance(content, str) or len(content) > 2000):
            raise ViewerError("字幕时间或文本无效。", code="vod.subtitle_invalid", level="WARN")
        text = "".join(c for c in content.replace("\r\n", "\n") if c >= " " or c == "\n").strip()
        if not text:continue
        lines.append(f"{len(lines) + 1}\n{stamp(start)} --> {stamp(end)}\n{text}\n")
    result = "\n".join(lines)
    if not result or len(result) > 2000000:
        raise ViewerError("字幕为空或过大。", code="vod.subtitle_invalid", level="WARN")
    return result


def download(subtitle, *, network_mode="direct"):
    with new_session(network_mode=network_mode) as session:
        url = subtitle.url
        for _ in range(4):
            try:
                with session.get(subtitle_url(url), stream=True, allow_redirects=False, timeout=(5, 15)) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = subtitle_url(urljoin(url, response.headers.get("Location", "")));continue
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_content(65536):
                        raw.extend(chunk)
                        if len(raw) > 4 * 1024 * 1024:raise ViewerError("字幕文件过大。", code="vod.subtitle_invalid", level="WARN")
                    import json
                    return to_srt(json.loads(raw.decode("utf-8-sig")))
            except (requests.RequestException, ValueError, UnicodeError):
                raise ViewerError("字幕读取失败，请重新获取字幕或选择不加载字幕。", code="vod.subtitle_unavailable", level="WARN") from None
    raise ViewerError("字幕重定向次数过多。", code="vod.subtitle_unavailable", level="WARN")
