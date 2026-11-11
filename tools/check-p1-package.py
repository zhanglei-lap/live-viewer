"""Verify the actual P1 EXE and release ZIP without using personal settings."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from live_viewer.app import VERSION

directory = ROOT / "dist" / "p1"
executable = directory / f"LiveViewer-{VERSION}.exe"
archive = directory / f"LiveViewer-{VERSION}-win-x64.zip"
output = ROOT / ".research" / "p1-packaged-validation.json"
run = subprocess.run([str(executable), "--self-test", str(output)],
                     cwd=directory, creationflags=subprocess.CREATE_NO_WINDOW, timeout=90, check=False)
result = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else {}
with zipfile.ZipFile(archive) as package:
    names = package.namelist()
    checks = {
        "exe_offline_check": run.returncode == 0 and result.get("success") is True and result.get("frozen") is True,
        "p1_checks": result.get("p1", {}).get("success") is True,
        "no_personal_data": not any(name.lower().startswith(("data/", ".local/", "credentials/", "logs/"))
                                   or "cookies" in name.lower() or name.lower().endswith(".dpapi") for name in names),
        "runtime_and_license": all(name in names for name in ("runtime/mpv/libmpv-2.dll",
                                                             "runtime/mpv/LICENSE.LGPL", "runtime/mpv/source.json")),
        "usage_and_acceptance": "Usage.md" in names and "P1.md" in names,
        "matching_executable": hashlib.sha256(package.read(executable.name)).digest() == hashlib.sha256(executable.read_bytes()).digest(),
        "zip_integrity": package.testzip() is None,
    }
report = {"success": all(checks.values()), "checks": checks, "version": VERSION,
          "zip": str(archive), "zip_bytes": archive.stat().st_size,
          "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
          "exe_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
target = ROOT / ".research" / "p1-package-artifacts.json"
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=True), flush=True)
raise SystemExit(0 if report["success"] else 1)
