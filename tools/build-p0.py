"""Build only the isolated Qt P0 prototype; never migrates production data."""
import hashlib
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-p0" / "Scripts" / "python.exe"


def main():
    if Path(sys.executable).resolve() != PYTHON.resolve():
        subprocess.run([str(PYTHON), str(Path(__file__).resolve())], cwd=ROOT, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return
    output_root = (ROOT / "dist" / "p0").resolve()
    work_root = (ROOT / "build" / "p0").resolve()
    if not output_root.is_relative_to(ROOT) or not work_root.is_relative_to(ROOT):
        raise RuntimeError("Prototype build targets must stay inside the workspace")
    import platform
    def no_wmi(*args, **kwargs):
        raise OSError("Build metadata uses native Windows fallback")
    platform._wmi_query = no_wmi
    os.environ["LIVE_VIEWER_BUILD_NO_WMI"] = "1"
    os.environ["PYTHONPATH"] = str(ROOT / "tools" / "build_support")
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".research" / "p0-pyinstaller-cache")
    # Dependency analysis must not collect incompatible same-name ICU libraries
    # from unrelated tools in the desktop environment's inherited PATH.
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    package_root = ROOT / ".venv-p0" / "Lib" / "site-packages"
    os.environ["PATH"] = os.pathsep.join(str(path) for path in (
        windows / "System32", windows, Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        PYTHON.parent, package_root / "shiboken6", package_root / "PySide6"))
    import PyInstaller.__main__
    print("Building the P0 Qt prototype...", flush=True)
    PyInstaller.__main__.run([
        "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
        "--name", "LiveViewer-P0", "--contents-directory", "runtime",
        "--exclude-module", "setuptools", "--exclude-module", "_distutils_hack",
        "--exclude-module", "pkg_resources",
        "--distpath", str(output_root), "--workpath", str(work_root),
        "--specpath", str(work_root), "--log-level", "WARN",
        "--runtime-hook", str(ROOT / "tools" / "p0-runtime-check-hook.py"),
        "--runtime-hook", str(ROOT / "tools" / "p0-runtime-check-hook.py"),
        "--add-data", str(ROOT / "prototypes" / "p0" / "theme.qss") + ";.",
        str(ROOT / "prototypes" / "p0" / "app.py")])
    package = output_root / "LiveViewer-P0"
    shutil.copyfile(ROOT / "prototypes" / "p0" / "README.md", package / "使用说明.md")
    shutil.copyfile(ROOT / "docs" / "p0" / "design-spec.md", package / "P0-设计规范.md")
    shutil.copyfile(ROOT / "docs" / "p0" / "event.schema.json", package / "事件结构.schema.json")
    source_dir = package / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    for name in ("app.py", "model.py", "validate.py", "theme.qss", "requirements.txt"):
        shutil.copyfile(ROOT / "prototypes" / "p0" / name, source_dir / name)
    licenses = package / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    for file in (ROOT / "vendor" / "p0-licenses").glob("*"):
        if file.is_file():
            shutil.copyfile(file, licenses / file.name)
    for name in ("PySide6-Essentials", "shiboken6"):
        distribution = importlib.metadata.distribution(name)
        for file in distribution.files:
            if "license" in str(file).lower() and str(file).lower().endswith(".txt"):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    shutil.copyfile(source, licenses / f"{name}-{source.name}")
    (package / "Runtime-Notes.txt").write_text(
        "P0 interaction prototype. All media/account state is simulated in memory.\n"
        "PySide6 Essentials / shiboken6 6.12.0; Qt Widgets.\n"
        "Qt / PySide6 are third-party components; license texts are in licenses/.\n"
        "Source: https://code.qt.io/cgit/pyside/pyside-setup.git/\n"
        "Qt source: https://code.qt.io/cgit/qt/qtbase.git/\n"
        "Application prototype source: prototypes/p0/ in this workspace.\n"
        "The adjacent runtime contains replaceable shared libraries.\n", encoding="utf-8")
    archive = output_root / "LiveViewer-P0-win-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for file in sorted(package.rglob("*")):
            if file.is_file():
                z.write(file, file.relative_to(output_root).as_posix())
    print("Executable: " + str(package / "LiveViewer-P0.exe"), flush=True)
    print("ZIP: " + str(archive), flush=True)
    print("SHA256: " + hashlib.sha256(archive.read_bytes()).hexdigest(), flush=True)


if __name__ == "__main__":
    main()
