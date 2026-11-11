"""Anonymous native timeshift probe; no Qt recovery to mask seek behavior."""
import json
from pathlib import Path
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live_viewer.core import PlatformClient
from live_viewer.relay import LocalRelay
from live_viewer.potplayer import PotPlayerAdapter, read_ini, write_ini
from potplayer_probe_support import original_settings_fingerprint
sys.stdout.reconfigure(encoding="utf-8")
base = ROOT / ".research/p5-timeshift-probe"
base.mkdir(parents=True, exist_ok=True)
adapter = PotPlayerAdapter(base, Path(r"D:\potplayer\PotPlayerMini64.exe"), hidden=True)
relay = None
report = {"success": False, "samples": [], "original_settings_unchanged": False}
before = original_settings_fingerprint()
try:
    adapter.prepare()
    ini = read_ini(adapter.profile.ini)
    ini["Settings"].update(UseStreamTimeShift="1", StreamTimeShiftTime="300")
    write_ini(adapter.profile.ini, ini)
    client = PlatformClient("douyu")
    stream = client.resolve("6657")
    relay = LocalRelay("douyu", "6657", None, initial_client=client, initial_stream=stream)
    relay.prepare_first_source();relay.start()
    adapter.open(relay.url, volume=0, mute=True)
    deadline = time.monotonic() + 20
    while (adapter.snapshot().get("state") != "playing") and time.monotonic() < deadline:
        time.sleep(.1)
    report["initial"] = adapter.snapshot()
    for _ in range(30):
        time.sleep(1)
        snapshot = adapter.snapshot()
        if adapter.native is not None:
            snapshot.update(duration_seconds=adapter.native.send(0x5002) / 1000,
                            progress_seconds=adapter.native.send(0x5003) / 1000)
        report["samples"].append(snapshot)
    position = adapter.snapshot().get("position_seconds", 0)
    adapter.native.send(0x5005, max(0, int((position - 15) * 1000)))
    time.sleep(1)
    report["after_seek"] = adapter.snapshot()
    adapter.pause();time.sleep(2)
    report["paused"] = adapter.snapshot()
    adapter.resume();time.sleep(1)
    report["continued"] = adapter.snapshot()
    report["native_files"] = [{"name": p.relative_to(adapter.profile.path).as_posix(), "bytes": p.stat().st_size}
                              for p in adapter.profile.path.rglob("*") if p.is_file() and p.suffix.lower() not in
                              (".dll", ".exe", ".png", ".bmp", ".ico", ".dsf", ".ini", ".txt")]
    report["success"] = report["after_seek"].get("position_seconds", position) < position - 5
except Exception as exc:
    report["error"] = type(exc).__name__
finally:
    if relay:
        relay.stop()
    adapter.close(wait=True)
    report["original_settings_unchanged"] = original_settings_fingerprint() == before
    (base / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": report["success"], "initial": report.get("initial"),
                      "last_sample": report["samples"][-1] if report["samples"] else None,
                      "after_seek": report.get("after_seek"), "continued": report.get("continued"),
                      "error": report.get("error")}, ensure_ascii=False))
