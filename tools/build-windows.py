"""Build the Windows app and a complete ZIP, leaving personal data untouched."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def call(arguments):
    subprocess.run(arguments, cwd=ROOT, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def main():
    print("Build entry started.", flush=True)
    if os.name != "nt":
        raise RuntimeError("Build the Windows executable on Windows.")
    os.chdir(ROOT)
    build_python = ROOT / ".venv-build" / "Scripts" / "python.exe"
    if Path(sys.executable).resolve() != build_python.resolve():
        if not build_python.is_file():
            call([sys.executable, "-m", "venv", str(ROOT / ".venv-build")])
        call([str(build_python), str(Path(__file__).resolve())])
        return
    if any(importlib.util.find_spec(name) is None for name in ("PyInstaller", "requests", "qrcode", "PIL")):
        call([str(build_python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(ROOT / "requirements-build.txt")])
    include_mpv = os.environ.get("LIVE_VIEWER_INCLUDE_MPV", "1") != "0"
    if include_mpv:
        print("Verifying the playback library...", flush=True)
        call([str(build_python), str(ROOT / "tools" / "fetch-mpv.py")])
    print("Building the executable...", flush=True)
    # Platform metadata lookup was observed hanging in WMI on this machine.
    # Scope the fallback to this build process and its analysis subprocesses.
    import platform
    def unavailable_wmi(*args, **kwargs):
        raise OSError("WMI metadata is unavailable during this build.")
    platform._wmi_query = unavailable_wmi
    os.environ["LIVE_VIEWER_BUILD_NO_WMI"] = "1"
    support = str(ROOT / "tools" / "build_support")
    os.environ["PYTHONPATH"] = support + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    sys.path.insert(0, str(ROOT))
    from live_viewer.app import VERSION
    from live_viewer.settings import StateStore
    import PyInstaller.__main__
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".local" / "pyinstaller-cache")
    name = "LiveViewer-" + VERSION + os.environ.get("LIVE_VIEWER_BUILD_SUFFIX", "")
    dist = (ROOT / "dist" / os.environ.get("LIVE_VIEWER_DIST_SUBDIR", "")).resolve()
    if not dist.is_relative_to((ROOT / "dist").resolve()):
        raise RuntimeError("Build output must stay within the workspace dist directory.")
    dist.mkdir(parents=True, exist_ok=True)
    PyInstaller.__main__.run(["--noconfirm", "--onefile", "--windowed", "--noupx", "--name", name,
                            "--exclude-module", "setuptools", "--exclude-module", "_distutils_hack",
                            "--exclude-module", "pkg_resources",
                            "--icon", "NONE", "--distpath", str(dist), "--workpath", str(ROOT / "build"),
                            "--specpath", str(ROOT / "build"), "--log-level", "WARN",
                            "--runtime-hook", str(ROOT / "tools" / "runtime-selftest-trace.py"), str(ROOT / "legacy_main.py")])
    target = StateStore(dist / "data")
    if not target.path.exists():
        from live_viewer.settings import sanitize_settings
        target.save(sanitize_settings({}))
    stage = "p2" if VERSION.endswith("-p2") else "p1" if VERSION.endswith("-p1") else None
    usage = ROOT / "docs" / (stage + "-usage.md" if stage else "portable-usage.md")
    shutil.copyfile(usage, dist / "Usage.md")
    if stage:
        shutil.copyfile(ROOT / "docs" / (stage + "-acceptance.md"), dist / (stage.upper() + ".md"))
    shutil.copyfile(ROOT / "docs" / "playback-and-network.md", dist / "Technical.md")
    runtime_files = []
    if include_mpv:
        runtime = dist / "runtime" / "mpv"
        runtime.mkdir(parents=True, exist_ok=True)
        for source, destination in [(ROOT / "vendor" / "libmpv" / file, runtime / file)
                                    for file in ("libmpv-2.dll", "LICENSE.LGPL", "source.json")]:
            if not destination.is_file() or hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                shutil.copyfile(source, destination)
        shutil.copyfile(ROOT / "vendor" / "libmpv" / "include" / "mpv" / "client.h", runtime / "client.h")
        shutil.copyfile(ROOT / "live_viewer" / "assets" / "live-controls.lua", runtime / "live-controls.lua")
        runtime_files = sorted((dist / "runtime").rglob("*"))
    archive_path = dist / (name + "-win-x64.zip")
    exe = dist / (name + ".exe")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        additional = [dist / (stage.upper() + ".md")] if stage else []
        for file in [exe, dist / "Usage.md", dist / "Technical.md", *additional, *runtime_files]:
            if file.is_file():
                archive.write(file, file.relative_to(dist).as_posix())
    print(json.dumps({"executable": str(exe), "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
                      "zip": str(archive_path)}, ensure_ascii=True), flush=True)


if __name__ == "__main__": main()
