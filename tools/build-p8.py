"""Build the Qt P8 onedir ZIP; exclude all user data and proprietary player files."""
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


def main():
    if 'VERSION = "0.9.0"' not in (ROOT / "live_viewer/qt/app.py").read_text(encoding="utf-8"):
        raise SystemExit("源码版本与 P8-B 发行名称不符，请使用对应阶段构建脚本。")
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
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".research/p8-pyinstaller-cache")
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    packages = PYTHON.parent.parent / "Lib/site-packages"
    os.environ["PATH"] = os.pathsep.join(str(p) for p in (
        windows / "System32", windows, Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        PYTHON.parent, packages / "shiboken6", packages / "PySide6"))
    import PyInstaller.__main__
    name = "LiveViewer-0.9.0"
    output = Path(os.environ.get("LIVE_VIEWER_P8_BUILD_OUTPUT", str(ROOT / "dist/p8"))).resolve()
    work = ROOT / "build/p8"
    if (output / name / "data").exists():
        raise SystemExit("构建目录中已有个人 data，请保留数据并使用新的输出目录再构建。")
    print("Building Qt P8...", flush=True)
    PyInstaller.__main__.run([
        "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
        "--name", name, "--contents-directory", "runtime",
        "--exclude-module", "setuptools", "--exclude-module", "_distutils_hack",
        "--exclude-module", "pkg_resources", "--exclude-module", "tkinter",
        "--distpath", str(output), "--workpath", str(work), "--specpath", str(work),
        "--log-level", "WARN", "--add-data", str(ROOT / "live_viewer/qt/theme.qss") + ";live_viewer/qt",
        str(ROOT / "qt_main.py")])
    folder = output / name
    shutil.copyfile(ROOT / "docs/p8-usage.md", folder / "使用说明.md")
    shutil.copyfile(ROOT / "docs/p8-acceptance.md", folder / "P8-验收.md")
    shutil.copyfile(ROOT / "docs/p5-usage.md", folder / "p5-usage.md")
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
                 "requirements-qt.txt", "requirements-qt-build.txt", "README.md", "Build-Live-Viewer.cmd",
                 "Start-Live-Viewer.cmd"):
        shutil.copyfile(ROOT / name, source / name)
    for relative in ("tools/build-p8.py", "tools/build_support/sitecustomize.py", "tools/check-p8-ui.py",
                     "docs/p8-usage.md", "docs/p8-acceptance.md", "docs/p8-bangumi-plan.md",
                     "docs/p8a-usage.md", "docs/p8a-acceptance.md", "docs/roadmap-qt-potplayer.md",
                     "docs/p5-usage.md"):
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
        "No user settings, account credentials or proprietary PotPlayer binaries are shipped.\n",
        encoding="utf-8")
    archive = output / "LiveViewer-0.9.0-win-x64.zip"
    # An old run's data may exist beside the EXE; enumerate only release assets.
    assets = [folder / "LiveViewer-0.9.0.exe", folder / "使用说明.md",
              folder / "P8-验收.md", folder / "Runtime-Notes.txt", folder / "p5-usage.md"]
    for sub in ("runtime", "licenses", "source"):
        assets.extend(file for file in (folder / sub).rglob("*") if file.is_file())
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for file in sorted(assets):
            package.write(file, file.relative_to(output).as_posix())
    print(str(archive), flush=True)
    print("SHA256: " + hashlib.sha256(archive.read_bytes()).hexdigest(), flush=True)


if __name__ == "__main__":
    main()


