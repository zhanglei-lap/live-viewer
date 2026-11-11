"""Build the Qt P9 (0.10.0) onedir ZIP; exclude all user data and proprietary player files."""
import hashlib
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-p0/Scripts/python.exe"

NAME = "LiveViewer-0.10.0"
VERSION = "0.10.0"
# 随包分发的文档：使用说明、验收、以及 P9 的调查与评估。
DOCS = {
    "docs/p9-usage.md": "使用说明.md",
    "docs/p9-acceptance.md": "P9-验收.md",
    "docs/p9a-acceptance.md": "P9A-抖音可行性验收.md",
    "docs/p9b-lls-assessment.md": "P9B-低延迟迁移评估.md",
    "docs/p9-latency-2026-11-11.md": "P9-延迟调查.md",
}


def main():
    expected = f'VERSION = "{VERSION}"'
    if expected not in (ROOT / "live_viewer/qt/app.py").read_text(encoding="utf-8"):
        raise SystemExit(f"源码版本与 P9 发行名称不符（期望 {VERSION}），请使用对应阶段构建脚本。")
    if Path(sys.executable).resolve() != PYTHON.resolve():
        subprocess.run([str(PYTHON), str(Path(__file__).resolve())], cwd=ROOT, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return
    import platform

    def no_wmi(*_, **__):
        raise OSError("Native Windows metadata fallback")

    platform._wmi_query = no_wmi
    os.environ["LIVE_VIEWER_BUILD_NO_WMI"] = "1"
    os.environ["PYTHONPATH"] = str(ROOT / "tools/build_support")
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".research/p9-pyinstaller-cache")
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    packages = PYTHON.parent.parent / "Lib/site-packages"
    os.environ["PATH"] = os.pathsep.join(str(p) for p in (
        windows / "System32", windows, Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        PYTHON.parent, packages / "shiboken6", packages / "PySide6"))
    import PyInstaller.__main__
    output = Path(os.environ.get("LIVE_VIEWER_P9_BUILD_OUTPUT", str(ROOT / "dist/p9"))).resolve()
    work = ROOT / "build/p9"
    if (output / NAME / "data").exists():
        raise SystemExit("构建目录中已有个人 data，请保留数据并使用新的输出目录再构建。")
    print("Building Qt P9 (0.10.0)...", flush=True)
    PyInstaller.__main__.run([
        "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
        "--name", NAME, "--contents-directory", "runtime",
        "--exclude-module", "setuptools", "--exclude-module", "_distutils_hack",
        "--exclude-module", "pkg_resources", "--exclude-module", "tkinter",
        "--distpath", str(output), "--workpath", str(work), "--specpath", str(work),
        "--log-level", "WARN", "--add-data", str(ROOT / "live_viewer/qt/theme.qss") + ";live_viewer/qt",
        str(ROOT / "qt_main.py")])
    folder = output / NAME
    for source, target in DOCS.items():
        shutil.copyfile(ROOT / source, folder / target)
    licenses = folder / "licenses"
    licenses.mkdir(exist_ok=True)
    for file in (ROOT / "vendor/p0-licenses").glob("*"):
        if file.is_file():
            shutil.copyfile(file, licenses / file.name)
    for package in ("PySide6-Essentials", "shiboken6", "requests", "urllib3", "certifi", "idna", "charset-normalizer",
                    "qrcode", "pillow", "colorama"):
        distribution = importlib.metadata.distribution(package)
        for file in distribution.files:
            if any(p in str(file).lower() for p in ("license", "copying")) and not str(file).endswith(".py"):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    shutil.copyfile(source, licenses / (package + "-" + source.name))
    source = folder / "source"
    source.mkdir(exist_ok=True)
    for file in (ROOT / "live_viewer").rglob("*"):
        if file.is_file() and file.suffix in (".py", ".qss", ".lua"):
            target = source / file.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
    for file in (ROOT / "tests").glob("*.py"):
        target = source / "tests" / file.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(file, target)
    for name in ("main.py", "qt_main.py", "legacy_main.py", "requirements.txt", "requirements-core.txt",
                 "requirements-qt.txt", "requirements-qt-build.txt", "requirements-build.txt", "README.md",
                 "Build-Live-Viewer.cmd", "Build-Single-EXE.cmd",
                 "Start-Live-Viewer.cmd", "Start-Qt-Live-Viewer.cmd"):
        shutil.copyfile(ROOT / name, source / name)
    for relative in ("tools/build-p9.py", "tools/build-p9-single.py", "tools/build_support/sitecustomize.py",
                     "tools/check-p8-ui.py", "tools/probe-douyin.py", "tools/probe-latency.py",
                     *DOCS.keys(),
                     "docs/roadmap-qt-potplayer.md", "docs/p9a-usage.md",
                     "docs/p8-usage.md", "docs/p8-acceptance.md"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    shutil.copytree(ROOT / "vendor/p0-licenses", source / "vendor/p0-licenses", dirs_exist_ok=True)
    (folder / "Runtime-Notes.txt").write_text(
        "Qt Widgets / PySide6 Essentials 6.12.0 / shiboken6 6.12.0.\n"
        "Shared libraries in runtime/ may be replaced with compatible builds.\n"
        "Application source and dependency requirements are in source/.\n"
        "Qt source: https://code.qt.io/cgit/qt/qtbase.git/ (v6.12.0)\n"
        "PySide source: https://code.qt.io/cgit/pyside/pyside-setup.git/ (v6.12.0)\n"
        "License texts: licenses/. PotPlayer is separately installed by the user.\n"
        "No user settings, account credentials or proprietary PotPlayer binaries are shipped.\n"
        "P9 adds Douyin live via HTTP-FLV; it does not include WebRTC (lls).\n",
        encoding="utf-8")
    archive = output / (NAME + "-win-x64.zip")
    # An old run's data may exist beside the EXE; enumerate only release assets.
    assets = [folder / (NAME + ".exe"), folder / "Runtime-Notes.txt"]
    assets = [path for path in assets if path.is_file()]
    assets.extend(folder / target for target in DOCS.values())
    for sub in ("runtime", "licenses", "source"):
        assets.extend(file for file in (folder / sub).rglob("*") if file.is_file())
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for file in sorted(set(assets)):
            package.write(file, file.relative_to(output).as_posix())
    print(str(archive), flush=True)
    print("SHA256: " + hashlib.sha256(archive.read_bytes()).hexdigest(), flush=True)


if __name__ == "__main__":
    main()
