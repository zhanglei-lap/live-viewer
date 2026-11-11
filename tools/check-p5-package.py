"""Validate the exact shipped Qt EXE, its anonymous media path and archive."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from potplayer_probe_support import original_settings_fingerprint

folder = ROOT / "dist/p5/LiveViewer-0.6.0"
exe = folder / "LiveViewer-0.6.0.exe"
archive = folder.parent / "LiveViewer-0.6.0-win-x64.zip"
before = original_settings_fingerprint()
reports = []
for mode, output_name, extra in (
    ("--validate", "p5-packaged-ui.json", ["--screenshots", str(ROOT / ".research/p5-packaged-screenshots")]),
    ("--probe-live", "p5-packaged-live.json", []),
    ("--probe-live", "p5-packaged-bilibili.json", ["--probe-platform", "bilibili", "--probe-room", "35"]),
    ("--probe-live", "p5-packaged-bilibili-live.json", ["--probe-platform", "bilibili", "--probe-room", "6"]),
    ("--probe-features", "p5-packaged-features.json", []),
):
    output = ROOT / ".research" / output_name
    with tempfile.TemporaryDirectory(prefix="p5-package-", dir=ROOT / ".research") as data:
        process = subprocess.run([str(exe), "--data-dir", data, mode, str(output), *extra],
                                 cwd=folder, creationflags=subprocess.CREATE_NO_WINDOW, timeout=240)
        report = json.loads(output.read_text(encoding="utf-8"))
    reports.append({"mode": mode, "file": output_name, "returncode": process.returncode,
                    "success": report["success"], "frozen": report["frozen"],
                    "played": report.get("played"), "error": report.get("error")})
    print(json.dumps(reports[-1]), flush=True)

with zipfile.ZipFile(archive) as package:
    names = package.namelist()
    prefix = folder.name + "/"
    relative_names = [name.removeprefix(prefix) for name in names]
    source_files = [file for file in (ROOT / "live_viewer").rglob("*")
                    if file.is_file() and file.suffix in (".py", ".qss", ".lua")]
    checks = {
        "exact_exe_offline_ui_and_live_probe": all(r["returncode"] == 0 and r["success"] and r["frozen"] for r in reports),
        "douyu_real_media_played": reports[1]["played"] is True,
        "bilibili_live_or_explicit_offline": reports[2]["success"],
        "bilibili_real_media_played": reports[3]["played"] is True,
        "matching_executable": hashlib.sha256(package.read(prefix + exe.name)).digest() == hashlib.sha256(exe.read_bytes()).digest(),
        "matching_application_source": all(package.read(prefix + "source/" + f.relative_to(ROOT).as_posix()) == f.read_bytes()
                                            for f in source_files),
        "archive_integrity": package.testzip() is None,
        "no_personal_data": not any(name.lower().startswith(("data/", ".local/", "credentials/", "logs/")) or
                                   name.lower().endswith((".dpapi", ".ini", ".dpl")) for name in relative_names),
        "no_proprietary_player": not any(Path(name).name.lower() in ("potplayer64.dll", "potplayermini64.exe", "liveviewerpot.exe")
                                        for name in relative_names),
        "runtime_styles_and_licenses": prefix + "runtime/live_viewer/qt/theme.qss" in names and
                                       prefix + "licenses/LGPL-3.0.txt" in names,
        "original_player_settings_unchanged": original_settings_fingerprint() == before,
    }
artifact = {"version": "0.6.0", "success": all(checks.values()), "checks": checks,
            "reports": reports, "zip": str(archive), "bytes": archive.stat().st_size,
            "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}
(ROOT / ".research/p5-package-artifacts.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
print(json.dumps(artifact), flush=True)
raise SystemExit(0 if artifact["success"] else 1)


