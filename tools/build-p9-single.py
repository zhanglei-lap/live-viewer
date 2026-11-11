"""Build a self-contained P9 (0.10.0) EXE using the verified folder release as a baseline."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-p0/Scripts/python.exe"
BASE = ROOT / "dist/p9/LiveViewer-0.10.0"
OUTPUT = ROOT / "dist/p9-single"
NAME = "LiveViewer-0.10.0-single"


def main():
    if Path(sys.executable).resolve() != PYTHON.resolve():
        subprocess.run([str(PYTHON), str(Path(__file__).resolve())], cwd=ROOT, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return
    if 'VERSION = "0.10.0"' not in (ROOT / "live_viewer/qt/app.py").read_text(encoding="utf-8"):
        raise SystemExit("源码版本不是 0.10.0，请使用对应版本构建脚本。")
    if not (BASE / "source").is_dir() or not (BASE / "licenses").is_dir():
        raise SystemExit("请先运行 tools/build-p9.py 生成文件夹版及对应源码、许可证。")
    if (OUTPUT / "data").exists():
        raise SystemExit("单文件输出目录已有个人 data，请先保留数据并在新的输出目录构建。")
    for file in (ROOT / "live_viewer").rglob("*"):
        if file.is_file() and file.suffix in (".py", ".qss", ".lua"):
            baseline = BASE / "source" / file.relative_to(ROOT)
            if not baseline.is_file() or baseline.read_bytes() != file.read_bytes():
                raise SystemExit("源码与已验证的文件夹版不一致，请先更新并验证文件夹版。")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    support = OUTPUT / "LiveViewer-0.10.0-source-and-licenses.zip"
    with zipfile.ZipFile(support, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for sub in ("source", "licenses"):
            for file in sorted((BASE / sub).rglob("*")):
                if not file.is_file():
                    continue
                relative = file.relative_to(BASE)
                current = ROOT / file.relative_to(BASE / sub) if sub == "source" else None
                archive.write(current if current is not None and current.is_file() else file, relative.as_posix())
        for relative in ("tools/build-p9-single.py", "Build-Single-EXE.cmd", "docs/p9-single-usage.md"):
            archive.write(ROOT / relative, "source/" + relative)
        python_license = Path(sys.base_prefix) / "LICENSE.txt"
        if python_license.is_file():
            archive.write(python_license, "licenses/Python-LICENSE.txt")

    import platform

    def no_wmi(*_, **__):
        raise OSError("Native Windows metadata fallback")

    platform._wmi_query = no_wmi
    os.environ["LIVE_VIEWER_BUILD_NO_WMI"] = "1"
    os.environ["PYTHONPATH"] = str(ROOT / "tools/build_support")
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".research/p9-single-pyinstaller-cache")
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    packages = PYTHON.parent.parent / "Lib/site-packages"
    os.environ["PATH"] = os.pathsep.join(str(p) for p in (
        windows / "System32", windows, Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        PYTHON.parent, packages / "shiboken6", packages / "PySide6"))
    import PyInstaller.__main__
    work = ROOT / "build/p9-single"
    print("Building self-contained Qt P9 EXE...", flush=True)
    PyInstaller.__main__.run([
        "--noconfirm", "--clean", "--onefile", "--windowed", "--noupx", "--name", NAME,
        "--exclude-module", "setuptools", "--exclude-module", "_distutils_hack",
        "--exclude-module", "pkg_resources", "--exclude-module", "tkinter",
        "--distpath", str(OUTPUT), "--workpath", str(work), "--specpath", str(work),
        "--log-level", "WARN", "--add-data", str(ROOT / "live_viewer/qt/theme.qss") + ";live_viewer/qt",
        "--add-data", str(support) + ";release",
        "--add-data", str(ROOT / "docs/p9-single-usage.md") + ";release",
        str(ROOT / "qt_main.py")])
    exe = OUTPUT / (NAME + ".exe")
    result = {"version": "0.10.0", "format": "onefile", "exe": str(exe), "bytes": exe.stat().st_size,
              "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(), "source_and_licenses": str(support),
              "baseline_exe_sha256": hashlib.sha256((BASE / "LiveViewer-0.10.0.exe").read_bytes()).hexdigest()}
    (ROOT / ".research/p9-single-build.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
