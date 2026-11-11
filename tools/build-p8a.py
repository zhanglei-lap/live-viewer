"""Independent test tool; never replaces the 0.8.0 application release."""
import hashlib
import importlib.metadata
import os
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    import platform
    def no_wmi(*_, **__):
        raise OSError("Native Windows metadata fallback")
    platform._wmi_query = no_wmi
    os.environ["LIVE_VIEWER_BUILD_NO_WMI"] = "1"
    os.environ["PYTHONPATH"] = str(ROOT / "tools/build_support")
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".research/p8a-pyinstaller-cache")
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    packages = Path(sys.executable).parent.parent / "Lib/site-packages"
    os.environ["PATH"] = os.pathsep.join(str(p) for p in (
        windows / "System32", windows, Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        Path(sys.executable).parent, packages / "shiboken6", packages / "PySide6"))
    import PyInstaller.__main__
    name = "BangumiValidation-P8A"
    output, work = ROOT / "dist/p8a", ROOT / "build/p8a"
    if (output / name / "data-p8a").exists():
        raise SystemExit("构建目录中已有用户测试数据，请先保留该目录并改用新的构建输出，避免覆盖结果。")
    PyInstaller.__main__.run([
        "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx", "--name", name,
        "--contents-directory", "runtime", "--distpath", str(output), "--workpath", str(work),
        "--paths", str(ROOT),
        "--specpath", str(work), "--log-level", "WARN",
        "--exclude-module", "setuptools", "--exclude-module", "_distutils_hack",
        "--exclude-module", "pkg_resources", "--exclude-module", "tkinter",
        "--add-data", str(ROOT / "live_viewer/qt/theme.qss") + ";live_viewer/qt",
        str(ROOT / "tools/validate-p8a.py")])
    folder = output / name
    shutil.copyfile(ROOT / "docs/p8a-usage.md", folder / "使用说明.md")
    acceptance = (ROOT / "docs/p8a-acceptance.md").read_text(encoding="utf-8")
    (folder / "P8-A-验收.md").write_text(acceptance.replace(
        "](p8-bangumi-plan.md)", "](source/docs/p8-bangumi-plan.md)").replace(
        "](p8a-usage.md)", "](使用说明.md)"), encoding="utf-8")
    licenses = folder / "licenses"
    licenses.mkdir(exist_ok=True)
    for file in (ROOT / "vendor/p0-licenses").glob("*"):
        if file.is_file():
            shutil.copyfile(file, licenses / file.name)
    for package in ("PySide6-Essentials", "shiboken6", "requests", "urllib3", "certifi", "idna", "charset-normalizer", "qrcode", "pillow", "colorama"):
        distribution = importlib.metadata.distribution(package)
        for file in distribution.files:
            if any(p in str(file).lower() for p in ("license", "copying")) and not str(file).endswith(".py"):
                asset = Path(distribution.locate_file(file))
                if asset.is_file():
                    shutil.copyfile(asset, licenses / (package + "-" + asset.name))
    source = folder / "source"
    for file in (ROOT / "live_viewer").rglob("*"):
        if file.is_file() and file.suffix in (".py", ".qss", ".lua"):
            target = source / file.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
    for relative in ("tools/build-p8a.py", "tools/validate-p8a.py", "tools/build_support/sitecustomize.py", "Validate-P8-A.cmd",
                     "requirements.txt", "requirements-core.txt", "requirements-qt.txt", "requirements-qt-build.txt",
                     "docs/p8a-usage.md", "docs/p8a-acceptance.md", "docs/p8-bangumi-plan.md",
                     "tests/test_bangumi.py", "tests/test_qt_bangumi_validation.py"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    shutil.copytree(ROOT / "vendor/p0-licenses", source / "vendor/p0-licenses", dirs_exist_ok=True)
    (folder / "Runtime-Notes.txt").write_text(
        "Independent P8-A validation tool; the stable LiveViewer release is unchanged.\n"
        "Qt/PySide6 Essentials and shiboken6 6.12.0, shared libraries in runtime/.\n"
        "Application source and requirements in source/, license texts in licenses/.\n"
        "Qt source: https://code.qt.io/cgit/qt/qtbase.git/ (v6.12.0).\n"
        "PySide source: https://code.qt.io/cgit/pyside/pyside-setup.git/ (v6.12.0).\n"
        "PotPlayer must be installed separately; no proprietary player, user data, credentials or reports are included.\n", encoding="utf-8")
    # Whitelisted assets only: exclude any data beside the executable.
    assets = [folder / (name + ".exe"), folder / "使用说明.md", folder / "P8-A-验收.md", folder / "Runtime-Notes.txt"]
    for sub in ("runtime", "licenses", "source"):
        assets += [f for f in (folder / sub).rglob("*") if f.is_file()]
    archive = output / (name + "-win-x64.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for file in sorted(assets):
            package.write(file, file.relative_to(output).as_posix())
    print(str(archive), flush=True)
    print("SHA256: " + hashlib.sha256(archive.read_bytes()).hexdigest(), flush=True)


if __name__ == "__main__":
    main()
