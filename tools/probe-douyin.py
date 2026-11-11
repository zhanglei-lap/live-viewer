"""P9-A：抖音直播取流可行性验证（只读，不写用户数据与凭据）。

目标是在改动产品代码之前，用真实网络证明下面几件事：
  1. 未登录能否拿到 ttwid 并稳定调用 webcast/room/web/enter
  2. 开播房间的 stream_url 到底是 live_core_sdk_data 新结构还是 flv_pull_url 旧结构
  3. 各档位对应的实际分辨率 / 码率 / 编码（h264 还是 h265）
  4. 真实 CDN host 清单，用于裁剪 MEDIA_DOMAINS 白名单
  5. 拉流是否真的返回可播放 FLV（含编码 ID）
  6. 同一地址在 10 / 30 / 60 分钟后是否仍可用

用法：
    .venv-p0\\Scripts\\python.exe tools/probe-douyin.py
    .venv-p0\\Scripts\\python.exe tools/probe-douyin.py --room 369324308707 --room https://live.douyin.com/xxxx
    .venv-p0\\Scripts\\python.exe tools/probe-douyin.py --expiry 10,30,60

证据写入 .research/p9a-probe-<时间戳>.json（已被 .gitignore 排除），不进入发行包。
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live_viewer.network import configure_session  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
HOME = "https://live.douyin.com/"
ENTER = "https://live.douyin.com/webcast/room/web/enter/"
REFERER = "https://live.douyin.com/"

# 参数清单固化为常量，便于日后比对平台改版。
ENTER_PARAMS = {
    "aid": "6383", "app_name": "douyin_web", "live_id": "1", "device_platform": "web",
    "language": "zh-CN", "enter_from": "web_live", "cookie_enabled": "true",
    "screen_width": "1920", "screen_height": "1080", "browser_language": "zh-CN",
    "browser_platform": "Win32", "browser_name": "Chrome", "browser_version": "140.0.0.0",
    "enter_source": "", "is_need_double_stream": "false",
}

# 抖音直播 FLV 视频编码 ID（FLV VideoTag 首字节低 4 位）。
CODEC_IDS = {2: "Sorenson H.263", 7: "AVC (H.264)", 12: "HEVC (H.265)"}

# 新结构档位键 → 面向用户的名称。顺序即优先级。
QUALITY_NAMES = {"origin": "原画", "uhd": "超清", "hd": "高清", "sd": "标清"}
# 旧结构 flv_pull_url 键。
LEGACY_QUALITY_NAMES = {"FULL_HD1": "原画", "HD1": "高清", "SD1": "标清", "SD2": "流畅"}


def redact(url: str) -> str:
    """只保留 scheme / host / 路径尾部，签名参数绝不入报告。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparsable>"
    path = parsed.path
    tail = path.rsplit("/", 1)[-1]
    tail = tail.split(".", 1)[0]
    return f"{parsed.scheme}://{parsed.hostname}/…/{tail}.<ext>"


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def new_session(network_mode: str) -> requests.Session:
    session = configure_session(requests.Session(), network_mode)
    session.headers.update({"User-Agent": UA, "Referer": REFERER})
    return session


def ensure_ttwid(session: requests.Session) -> dict:
    """未登录访问首页即可获得 ttwid；这是后续 enter 请求的唯一前提。"""
    started = time.monotonic()
    try:
        response = session.get(HOME, timeout=(8, 20))
    except requests.RequestException as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    cookies = {c.name: c.value for c in session.cookies}
    return {
        "ok": response.status_code == 200 and "ttwid" in cookies,
        "status": response.status_code,
        "page_bytes": len(response.content),
        "cookie_names": sorted(cookies),
        "has_ttwid": "ttwid" in cookies,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def discover(session: requests.Session) -> list[str]:
    """从首页 SSR 数据里找候选 web_rid；只找得到少量，真实开播房间建议手动给。"""
    try:
        text = session.get(HOME, timeout=(8, 20)).text
    except requests.RequestException:
        return []
    patterns = (
        r"web_rid=(\d{6,20})",
        r'"webRid"\s*:\s*"(\d{6,20})"',
        r'\\"web_rid\\":\\"(\d{6,20})\\"',
        r'live\.douyin\.com/(\d{8,20})',
    )
    found: list[str] = []
    for pattern in patterns:
        for rid in re.findall(pattern, text):
            if rid not in found:
                found.append(rid)
    return found


def rid_from_input(value: str) -> str:
    """接受裸数字、live.douyin.com 链接、v.douyin.com 短链（短链需外部先解析）。"""
    value = value.strip()
    if value.isdigit():
        return value
    parsed = urlparse(value)
    if parsed.hostname and "douyin.com" not in parsed.hostname:
        raise ValueError(f"不是抖音链接：{parsed.hostname}")
    candidate = parsed.path.strip("/").split("/")[0]
    return candidate if candidate.isdigit() else ""


def enter(session: requests.Session, web_rid: str) -> dict:
    params = dict(ENTER_PARAMS, web_rid=web_rid, room_id_str="")
    started = time.monotonic()
    try:
        response = session.get(ENTER, params=params,
                               headers={"Referer": f"https://live.douyin.com/{web_rid}"},
                               timeout=(8, 20))
    except requests.RequestException as exc:
        return {"http": 0, "error": type(exc).__name__, "elapsed_ms": int((time.monotonic() - started) * 1000)}
    result = {"http": response.status_code, "bytes": len(response.content),
              "elapsed_ms": int((time.monotonic() - started) * 1000)}
    try:
        payload = response.json()
    except ValueError:
        result["body_prefix"] = response.text[:120]
        return result
    result["status_code"] = payload.get("status_code")
    data = payload.get("data") or {}
    result["prompts"] = str(data.get("prompts") or "")[:80]
    rooms = data.get("data") or []
    result["rooms"] = len(rooms)
    result["payload"] = payload
    return result


def parse_sdk_params(raw: str) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}


def extract_layout(stream_url: dict) -> dict:
    """同时支持 live_core_sdk_data 新结构与 flv_pull_url 旧结构。"""
    report = {"layout": None, "qualities": [], "hosts": [], "urls": []}
    core = stream_url.get("live_core_sdk_data") or {}
    raw = (core.get("pull_data") or {}).get("stream_data")
    if raw:
        try:
            decoded = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            decoded = {}
        tiers = decoded.get("data") or {}
        if tiers:
            report["layout"] = "live_core_sdk_data"
            for key, tier in tiers.items():
                main = (tier or {}).get("main") or {}
                backup = (tier or {}).get("backup") or {}
                params = parse_sdk_params(main.get("sdk_params") or "")
                for stream in (main, backup):
                    for url in (stream.get("flv"), stream.get("hls")):
                        if url:
                            report["urls"].append(url)
                            host = host_of(url)
                            if host and host not in report["hosts"]:
                                report["hosts"].append(host)
                report["qualities"].append({
                    "key": key,
                    "name": QUALITY_NAMES.get(key, key),
                    "sdk_key": params.get("sdk_key"),
                    "vcodec": params.get("vCodec"),
                    "resolution": params.get("resolution"),
                    "fps": params.get("fps"),
                    "vbitrate": params.get("vBitRate"),
                    "has_flv": bool(main.get("flv")),
                    "has_hls": bool(main.get("hls")),
                    "has_backup": bool(backup.get("flv")),
                })
            return report
    legacy = stream_url.get("flv_pull_url") or {}
    if legacy:
        report["layout"] = "flv_pull_url"
        for key, url in legacy.items():
            if url:
                report["urls"].append(url)
                host = host_of(url)
                if host and host not in report["hosts"]:
                    report["hosts"].append(host)
            report["qualities"].append({"key": key, "name": LEGACY_QUALITY_NAMES.get(key, key),
                                        "has_flv": bool(url)})
        hls = stream_url.get("hls_pull_url_map") or {}
        for url in hls.values():
            if url:
                report["urls"].append(url)
                host = host_of(url)
                if host and host not in report["hosts"]:
                    report["hosts"].append(host)
    return report


def flv_probe(session: requests.Session, url: str, seconds: float) -> dict:
    """真实拉流：确认返回 FLV、能读出编码 ID 与首个视频标签。"""
    result = {"ok": False, "codec_id": None, "codec": None, "bytes": 0}
    started = time.monotonic()
    try:
        response = session.get(url, headers={"Referer": REFERER}, stream=True, timeout=(8, 20))
    except requests.RequestException as exc:
        result["error"] = type(exc).__name__
        return result
    try:
        result["http"] = response.status_code
        result["content_type"] = response.headers.get("Content-Type")
        if response.status_code != 200:
            return result
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(16384):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= 262144 or time.monotonic() - started >= seconds:
                break
        data = b"".join(chunks)
        result["bytes"] = len(data)
        result["ok"] = data[:3] == b"FLV"
        if not result["ok"]:
            result["head"] = data[:16].hex()
            return result
        header_size = struct.unpack(">I", data[5:9])[0]
        position = header_size + 4
        tags = 0
        while position + 11 <= len(data) and tags < 400:
            tag_type = data[position]
            size = int.from_bytes(data[position + 1:position + 4], "big")
            body = data[position + 11:position + 11 + size]
            if tag_type == 9 and body:
                codec_id = body[0] & 0x0F
                if result["codec_id"] is None:
                    result["codec_id"] = codec_id
                    result["codec"] = CODEC_IDS.get(codec_id, f"未知({codec_id})")
                    result["has_sequence_header"] = body[1] == 0
            position += 11 + size + 4
            tags += 1
        result["tags_scanned"] = tags
    finally:
        response.close()
    return result


def raw_walk(payload: dict) -> dict:
    """取第一个房间的核心字段，供结构记录。"""
    data = payload.get("data") or {}
    rooms = data.get("data") or []
    if not rooms:
        return {"empty": True, "prompts": str(data.get("prompts") or "")[:80]}
    room = rooms[0]
    user = data.get("user") or {}
    stream_url = room.get("stream_url") or {}
    return {
        "empty": False,
        "status_code": payload.get("status_code"),
        "room_status": room.get("status"),
        "room_status_str": str(room.get("status_str") or "")[:40],
        "room_id": room.get("id_str"),
        "title": str(room.get("title") or "")[:40],
        "nickname": str(user.get("nickname") or "")[:30],
        "web_rid": user.get("web_rid"),
        "stream_url_keys": sorted(stream_url.keys()),
        "user_count": room.get("user_count_str"),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="P9-A 抖音取流可行性验证")
    parser.add_argument("--room", action="append", default=[], help="房间号或直播间链接，可重复")
    parser.add_argument("--attempts", type=int, default=20, help="取流接口连续调用次数")
    parser.add_argument("--seconds", type=float, default=8.0, help="每个地址的拉流采样秒数")
    parser.add_argument("--expiry", default="", help="有效期复测间隔（分钟，逗号分隔，例如 10,30,60）")
    parser.add_argument("--network-mode", choices=("direct", "system"), default="direct")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output or ROOT / f".research/p9a-probe-{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    report: dict = {"started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "network_mode": args.network_mode, "checks": {}, "rooms": [], "hosts": []}
    session = new_session(args.network_mode)

    print("[1/6] 获取 ttwid …", flush=True)
    report["checks"]["ttwid"] = ensure_ttwid(session)
    print("      →", json.dumps(report["checks"]["ttwid"], ensure_ascii=False), flush=True)
    if not report["checks"]["ttwid"].get("ok"):
        print("!! 未拿到 ttwid，后续步骤无法进行。", flush=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2

    candidates = [rid_from_input(v) for v in args.room]
    candidates = [c for c in candidates if c]
    if not candidates:
        print("[2/6] 未指定 --room，尝试从首页自动发现 …", flush=True)
        candidates = discover(session)
        report["checks"]["discovered"] = candidates
    print(f"      候选房间：{candidates or '（无）'}", flush=True)

    print(f"[3/6] 取流接口连续调用 {args.attempts} 次 …", flush=True)
    attempts = []
    target = candidates[0] if candidates else ""
    for index in range(args.attempts):
        outcome = enter(session, target) if target else {"http": 0, "error": "no_candidate"}
        attempts.append({k: v for k, v in outcome.items() if k != "payload"})
        if index < 5 or index == args.attempts - 1:
            print(f"      #{index + 1:02d} http={outcome.get('http')} "
                  f"status_code={outcome.get('status_code')} rooms={outcome.get('rooms')} "
                  f"{outcome.get('elapsed_ms')}ms", flush=True)
        time.sleep(0.6)
    codes = [a.get("status_code") for a in attempts]
    report["checks"]["stability"] = {
        "attempts": len(attempts),
        "ok": sum(1 for c in codes if c == 0),
        "http_444": sum(1 for a in attempts if a.get("http") == 444),
        "http_other": sum(1 for a in attempts if a.get("http") not in (0, 200)),
        "distinct_status_codes": sorted({str(c) for c in codes}),
        "samples": attempts[:5],
    }
    print("      →", json.dumps(report["checks"]["stability"], ensure_ascii=False), flush=True)

    print("[4/6] 解析房间与流结构 …", flush=True)
    hosts: list[str] = []
    for rid in candidates:
        outcome = enter(session, rid)
        payload = outcome.get("payload")
        if not payload:
            report["rooms"].append({"web_rid": rid, "error": outcome.get("error") or outcome.get("http")})
            continue
        summary = raw_walk(payload)
        summary["web_rid_input"] = rid
        room = ((payload.get("data") or {}).get("data") or [{}])[0]
        layout = extract_layout(room.get("stream_url") or {})
        summary["layout"] = layout["layout"]
        summary["qualities"] = layout["qualities"]
        summary["hosts"] = layout["hosts"]
        summary["url_count"] = len(layout["urls"])
        for host in layout["hosts"]:
            if host not in hosts:
                hosts.append(host)
        report["rooms"].append(summary)
        print(f"      web_rid={rid} status={summary.get('room_status')} "
              f"layout={summary.get('layout')} 档位={[q['key'] for q in layout['qualities']]} "
              f"hosts={layout['hosts']}", flush=True)
        # 只对第一个开播房间做真实拉流，避免产生过多请求
        if layout["urls"] and not any(r.get("flv") for r in report["rooms"]):
            print(f"[5/6] 真实拉流采样 {args.seconds}s …", flush=True)
            probe = None
            for url in layout["urls"][:4]:
                if not url.lower().split("?")[0].endswith(".flv"):
                    continue
                result = flv_probe(session, url, args.seconds)
                result["host"] = host_of(url)
                result["redacted"] = redact(url)
                print(f"      {result['host']}: ok={result['ok']} bytes={result['bytes']} "
                      f"codec={result['codec']} ct={result.get('content_type')}", flush=True)
                if result.get("ok") and probe is None:
                    probe = result
                if result.get("ok"):
                    break
            report["rooms"][-1]["flv"] = probe or {"ok": False}
            if probe:
                break
    report["hosts"] = hosts

    minutes = [int(x) for x in args.expiry.split(",") if x.strip().isdigit()]
    if minutes and any(r.get("flv") for r in report["rooms"]):
        print(f"[6/6] 地址有效期复测：{minutes} 分钟 …", flush=True)
        room = next(r for r in report["rooms"] if r.get("flv"))
        rid = room["web_rid_input"]
        expiry = []
        for minutes_ahead in minutes:
            time.sleep(minutes_ahead * 60 if not expiry else (minutes_ahead - expiry[-1]["after_minutes"]) * 60)
            fresh = enter(session, rid)
            room_data = ((fresh.get("payload") or {}).get("data") or {}).get("data") or [{}]
            layout = extract_layout(room_data[0].get("stream_url") or {}) if room_data else {"urls": []}
            expiry.append({"after_minutes": minutes_ahead,
                           "http": fresh.get("http"),
                           "status_code": fresh.get("status_code"),
                           "url_count": len(layout.get("urls") or [])})
            print("      →", expiry[-1], flush=True)
        report["checks"]["expiry"] = expiry
    else:
        print("[6/6] 跳过有效期复测（未指定 --expiry 或无可用开播房间）。", flush=True)

    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n证据已写入：{output}", flush=True)

    live = [r for r in report["rooms"] if not r.get("empty") and r.get("layout")]
    print(f"结论：候选 {len(candidates)} 个，拿到流结构 {len(live)} 个，"
          f"真实拉流成功 {sum(1 for r in live if (r.get('flv') or {}).get('ok'))} 个")
    return 0 if live else 1


if __name__ == "__main__":
    raise SystemExit(main())
