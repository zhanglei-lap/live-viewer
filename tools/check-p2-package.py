"""Check the exact shipped P2 executable and archive using isolated data."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from live_viewer.app import VERSION

folder = ROOT / "dist/p2"
exe = folder / f"LiveViewer-{VERSION}.exe"
archive = folder / f"LiveViewer-{VERSION}-win-x64.zip"
output = ROOT / ".research/p2-packaged-validation.json"
result = subprocess.run([str(exe), "--self-test", str(output)], cwd=folder,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=90)
report = json.loads(output.read_text(encoding="utf-8"))
with zipfile.ZipFile(archive) as package:
    names = package.namelist()
    checks = {
        "exe_self_test": result.returncode == 0 and report["success"] and report["frozen"],
        "p1_and_p2": report["p1"]["success"] and report["p2"]["success"],
        "optional_mpv_not_required": report.get("mpv_optional_not_installed") is True,
        "no_personal_data_or_player_binaries": not any(
            name.lower().startswith(("data/", ".local/", "credentials/", "logs/", "runtime/"))
            or name.lower().endswith(".dpapi") for name in names),
        "matching_executable": hashlib.sha256(package.read(exe.name)).digest() == hashlib.sha256(exe.read_bytes()).digest(),
        "usage_and_acceptance": all(name in names for name in ("Usage.md", "Technical.md", "P2.md")),
        "archive_integrity": package.testzip() is None,
    }
artifact = {"version": VERSION, "success": all(checks.values()), "checks": checks,
            "zip": str(archive), "bytes": archive.stat().st_size,
            "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}
(ROOT / ".research/p2-package-artifacts.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
print(json.dumps(artifact))
raise SystemExit(0 if artifact["success"] else 1)
