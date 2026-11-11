"""延迟探针：分别量化「转发层」与「PotPlayer」各自的缓冲深度。

背景：用户反馈抖音直播比网页版慢约 3 秒。现有报告里的 timing 只描述转发进度，
明确标注 player_display_latency_measured=false，因此无法归因。本探针补齐两项：

  转发层延迟  = stream_progress_gap_seconds（CDN 已到 vs 已写出播放器的 FLV 时间戳差）
  播放器缓冲  = forwarded_media_seconds - PotPlayer 报告的播放位置

同一房间分别以「原生时移开」和「原生时移关」各测一次，两者之差即位移缓冲的代价。

用法：
    .venv-p0\\Scripts\\python.exe tools/probe-latency.py `
        --platform douyin --room 67876457927 --quality 6 `
        --player D:\\potplayer\\PotPlayerMini64.exe --seconds 30

只读：使用临时数据目录，不读写用户设置与登录，不修改用户自己的 PotPlayer 配置。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live_viewer.core import PlatformClient  # noqa: E402
from live_viewer.potplayer import PotPlayerAdapter  # noqa: E402
from live_viewer.relay import LocalRelay, RelayEndpoint  # noqa: E402
from live_viewer.rtmp import ReplayRelay, RtmpEndpoint  # noqa: E402


def median(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(values), 3) if values else None


def run_once(platform, room, requested, player_path, directory, *, replay, seconds, warmup,
             replay_minutes=300):
    events = []
    client = PlatformClient(platform)
    http_endpoint = RelayEndpoint()
    rtmp_endpoint = RtmpEndpoint() if replay else None
    adapter = PotPlayerAdapter(directory, player_path, hidden=True)
    relay = None
    label = ("原生时移开（本地 RTMP，" + str(replay_minutes) + " 分钟）" if replay
             else "原生时移关（直连 HTTP-FLV）")
    print(f"\n=== {label} ===", flush=True)
    try:
        stream = client.resolve(room, requested)
        print(f"档位：请求 {requested} -> 实际 {stream.actual} {stream.actual_name} "
              f"| 编码 {stream.codec} | {len(stream.urls)} 个地址", flush=True)
        adapter.prepare(replay=replay, replay_minutes=replay_minutes, vsr=False)
        common = dict(low_latency=True, network_mode="direct", initial_client=client,
                      initial_stream=stream, endpoint=http_endpoint,
                      on_event=lambda code, message, **_: events.append(code))
        if replay:
            relay = ReplayRelay(platform, room, requested, None, replay_endpoint=rtmp_endpoint, **common)
        else:
            relay = LocalRelay(platform, room, requested, None, **common)
        relay.prepare_first_source()
        relay.start()
        url = relay.url
        print(f"本机地址：{url.split('?')[0][:64]}", flush=True)
        adapter.open(url, "延迟探针", volume=0, mute=True)

        deadline = time.monotonic() + warmup
        state = None
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                snapshot = adapter.snapshot()
                state = snapshot.get("state")
            except Exception:
                continue
        print(f"预热 {warmup}s 后状态：{state}", flush=True)

        samples = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(1)
            timing = relay.snapshot().get("timing") or {}
            try:
                player = adapter.snapshot()
            except Exception:
                player = {}
            forwarded = timing.get("forwarded_media_seconds")
            position = player.get("position_seconds")
            samples.append({
                "forwarded": forwarded,
                "received": timing.get("received_media_seconds"),
                "relay_gap": timing.get("stream_progress_gap_seconds"),
                "queue_seconds": timing.get("pending_seconds"),
                "position": position,
                "state": player.get("state"),
                "player_buffer": (round(forwarded - position, 3)
                                  if isinstance(forwarded, (int, float)) and isinstance(position, (int, float)) else None),
            })
        if not samples:
            return {"label": label, "replay": replay, "error": "没有采到样本"}
        buffers = [s["player_buffer"] for s in samples if isinstance(s["player_buffer"], (int, float))]
        return {
            "label": label,
            "replay": replay,
            "stream": {"actual": stream.actual, "name": stream.actual_name, "codec": stream.codec},
            "samples": len(samples),
            "player_state": samples[-1]["state"],
            "relay_gap_seconds": median([s["relay_gap"] for s in samples]),
            "relay_queue_seconds": median([s["queue_seconds"] for s in samples]),
            "received_minus_forwarded_seconds": median(
                [round(s["received"] - s["forwarded"], 3)
                 for s in samples if isinstance(s["received"], (int, float)) and isinstance(s["forwarded"], (int, float))]),
            "player_buffer_seconds": median(buffers),
            "player_buffer_first_seconds": buffers[0] if buffers else None,
            "player_buffer_last_seconds": buffers[-1] if buffers else None,
            # 增长为正说明播放器越落越远（积压）；接近 0 说明是恒定缓冲，只影响固定偏移。
            "player_buffer_growth_seconds": round(buffers[-1] - buffers[0], 3) if len(buffers) > 1 else None,
            "player_buffer_max_seconds": max(buffers) if buffers else None,
            "series": [round(b, 2) if isinstance(b, (int, float)) else None for b in buffers],
            "events": sorted(set(events)),
        }
    finally:
        try:
            adapter.close(wait=True)
        except Exception:
            pass
        if relay is not None:
            try:
                relay.stop()
            except Exception:
                pass
        try:
            http_endpoint.close()
        except Exception:
            pass
        if rtmp_endpoint is not None:
            try:
                rtmp_endpoint.close()
            except Exception:
                pass
        client.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="转发层与 PotPlayer 缓冲深度探针")
    parser.add_argument("--platform", default="douyin")
    parser.add_argument("--room", required=True)
    parser.add_argument("--quality", type=int, default=None, help="请求档位；留空用平台默认")
    parser.add_argument("--player", type=Path, default=Path(r"D:\potplayer\PotPlayerMini64.exe"))
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--only", choices=("replay", "direct"), help="只测一种链路")
    parser.add_argument("--replay-minutes", type=int, default=300,
                        help="时移缓冲长度（对应设置里的最长回看范围，10-300）")
    parser.add_argument("--output", type=Path, default=ROOT / ".research/latency-probe.json")
    args = parser.parse_args()

    if not args.player.is_file():
        print(f"找不到播放器：{args.player}", flush=True)
        return 2
    modes = {"replay": [True], "direct": [False]}.get(args.only, [True, False])
    report = {"platform": args.platform, "room": args.room, "player": str(args.player),
              "seconds": args.seconds, "warmup": args.warmup, "runs": []}
    for replay in modes:
        with tempfile.TemporaryDirectory(prefix="latency-", dir=ROOT / ".research") as temporary:
            report["runs"].append(run_once(args.platform, args.room, args.quality, args.player,
                                           Path(temporary), replay=replay,
                                           seconds=args.seconds, warmup=args.warmup,
                                           replay_minutes=args.replay_minutes))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 92, flush=True)
    print(f"{'链路':32}{'转发滞后':>9}{'队列':>7}{'缓冲中位':>9}{'首样本':>8}{'末样本':>8}{'增长':>8}", flush=True)
    for run in report["runs"]:
        if run.get("error"):
            print(f"{run['label']:32}{run['error']:>40}", flush=True)
            continue
        print(f"{run['label']:32}{str(run['relay_gap_seconds']):>9}"
              f"{str(run['relay_queue_seconds']):>7}{str(run['player_buffer_seconds']):>9}"
              f"{str(run['player_buffer_first_seconds']):>8}{str(run['player_buffer_last_seconds']):>8}"
              f"{str(run['player_buffer_growth_seconds']):>8}", flush=True)
    for run in report["runs"]:
        if run.get("series"):
            print(f"\n{run['label']} 缓冲时间序列：", flush=True)
            print("  " + " ".join(str(v) for v in run["series"]), flush=True)
    print(f"\n证据：{args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
