"""Build the Qt P3 onedir ZIP; exclude all user data and proprietary player files."""
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
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".research/p3-pyinstaller-cache")
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    packages = PYTHON.parent.parent / "Lib/site-packages"
    os.environ["PATH"] = os.pathsep.join(str(p) for p in (
        windows / "System32", windows, Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        PYTHON.parent, packages / "shiboken6", packages / "PySide6"))
    import PyInstaller.__main__
    sys.path.insert(0, str(ROOT))
    from live_viewer.qt.app import VERSION
    if VERSION != "0.5.3-p3":
        raise RuntimeError("当前源码已升级，请使用 tools/build-p4.py；原 P3 包不会被覆盖。")
    name = "LiveViewer-0.5.3-p3"
    output = ROOT / "dist/p3"
    work = ROOT / "build/p3"
    print("Building Qt P3...", flush=True)
    PyInstaller.__main__.run([
        "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
        "--name", name, "--contents-directory", "runtime",
        "--exclude-module", "setuptools", "--exclude-module", "_distutils_hack",
        "--exclude-module", "pkg_resources", "--exclude-module", "tkinter",
        "--distpath", str(output), "--workpath", str(work), "--specpath", str(work),
        "--log-level", "WARN", "--add-data", str(ROOT / "live_viewer/qt/theme.qss") + ";live_viewer/qt",
        str(ROOT / "qt_main.py")])
    folder = output / name
    shutil.copyfile(ROOT / "docs/p3-usage.md", folder / "使用说明.md")
    shutil.copyfile(ROOT / "docs/p3-acceptance.md", folder / "P3-验收.md")
    licenses = folder / "licenses"
    licenses.mkdir(exist_ok=True)
    for file in (ROOT / "vendor/p0-licenses").glob("*"):
        if file.is_file():
            shutil.copyfile(file, licenses / file.name)
    for package in ("PySide6-Essentials", "shiboken6", "requests", "urllib3", "certifi", "idna", "charset-normalizer"):
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
    for name in ("qt_main.py", "requirements.txt", "requirements-qt.txt", "requirements-qt-build.txt"):
        shutil.copyfile(ROOT / name, source / name)
    for relative in ("tools/build-p3.py", "tools/build_support/sitecustomize.py",
                     "docs/p3-usage.md", "docs/p3-acceptance.md"):
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
    archive = output / "LiveViewer-0.5.3-p3-win-x64.zip"
    # An old run's data may exist beside the EXE; enumerate only release assets.
    assets = [folder / "LiveViewer-0.5.3-p3.exe", folder / "使用说明.md",
              folder / "P3-验收.md", folder / "Runtime-Notes.txt"]
    for sub in ("runtime", "licenses", "source"):
        assets.extend(file for file in (folder / sub).rglob("*") if file.is_file())
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for file in sorted(assets):
            package.write(file, file.relative_to(output).as_posix())
    print(str(archive), flush=True)
    print("SHA256: " + hashlib.sha256(archive.read_bytes()).hexdigest(), flush=True)


if __name__ == "__main__":
    main()
