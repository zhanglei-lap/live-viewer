"""Verify a sole EXE with isolated data, temp folders, working directory and PATH."""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist/p8-single/LiveViewer-0.9.0-single.exe"
BASE = ROOT / "dist/p8/LiveViewer-0.9.0"
OUTPUT = ROOT / ".research/p8-single-validation.json"


def owned_windows(executable):
    user, kernel = C.WinDLL("user32", use_last_error=True), C.WinDLL("kernel32", use_last_error=True)
    callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user.EnumWindows.argtypes = [callback, W.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
    user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
    user.IsWindowVisible.argtypes = [W.HWND]
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    windows = []
    @callback
    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        pid = W.DWORD()
        user.GetWindowThreadProcessId(hwnd, C.byref(pid))
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                path, length = C.create_unicode_buffer(32768), W.DWORD(32768)
                if kernel.QueryFullProcessImageNameW(handle, 0, path, C.byref(length)) and Path(path.value).resolve() == executable:
                    title = C.create_unicode_buffer(256)
                    user.GetWindowTextW(hwnd, title, 256)
                    windows.append((hwnd, title.value, pid.value))
            finally:
                kernel.CloseHandle(handle)
        return True
    user.EnumWindows(visit, 0)
    return windows


def close_owned_windows(executable):
    user = C.WinDLL("user32", use_last_error=True)
    user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    for hwnd, _, _ in owned_windows(executable):
        user.PostMessageW(hwnd, 0x0010, 0, 0)


def main():
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    (ROOT / ".research").mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="单文件验证 ", dir=ROOT / ".research"))
    bare = directory / "只有 EXE"
    working = directory / "其他工作目录"
    temporary = directory / "临时依赖"
    for path in (bare, working, temporary):
        path.mkdir()
    executable = bare / EXE.name
    shutil.copyfile(EXE, executable)
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.upper().startswith(("PYTHON", "QT_", "PYSIDE")):
            environment.pop(key)
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    environment.update(PATH=os.pathsep.join((str(windows / "System32"), str(windows))), TEMP=str(temporary), TMP=str(temporary))
    result = {"success": False, "format": "onefile", "exe_sha256": hashlib.sha256(EXE.read_bytes()).hexdigest(),
              "directory": str(directory), "checks": {}, "reports": {}}
    def save():
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    def check(name, condition):
        result["checks"][name] = bool(condition)
        save()
        print(name, bool(condition), flush=True)
        if not condition:
            raise RuntimeError(name)
    process = None
    try:
        check("only_exe_copied", [p.name for p in bare.iterdir()] == [EXE.name])
        archive = CArchiveReader(str(executable))
        baseline = CArchiveReader(str(BASE / "LiveViewer-0.9.0.exe"))
        check("same_business_code_as_verified_p8", all(archive.extract(k) == baseline.extract(k) for k in ("PYZ.pyz", "qt_main")))
        count = 0
        for file in (BASE / "runtime").rglob("*"):
            if file.is_file() and file.suffix.lower() in (".dll", ".pyd"):
                key = str(file.relative_to(BASE / "runtime"))
                check("embedded_" + key, key in archive.toc and archive.extract(key) == file.read_bytes())
                count += 1
        result["verified_embedded_binary_count"] = count
        check("source_licenses_and_style_embedded", "release\\LiveViewer-0.9.0-source-and-licenses.zip" in archive.toc and
              "live_viewer\\qt\\theme.qss" in archive.toc)

        started = time.monotonic()
        process = subprocess.Popen([str(executable)], cwd=working, env=environment)
        found = []
        while process.poll() is None and time.monotonic() - started < 40:
            found = [w for w in owned_windows(executable) if w[1] == "本地直播 · 0.9.0"]
            if found:
                break
            time.sleep(.1)
        check("normal_gui_startup_without_external_runtime", bool(found))
        result["normal_startup_seconds"] = round(time.monotonic() - started, 2)
        check("onefile_temp_runtime_created", any(temporary.glob("_MEI*")))
        close_owned_windows(executable)
        check("normal_exit", process.wait(timeout=30) == 0)
        check("default_data_beside_exe", (bare / "data/settings.json").is_file())
        check("onefile_temp_runtime_cleaned", not any(temporary.glob("_MEI*")))

        cases = [
            ("offline", ["--validate", str(directory / "offline.json"), "--screenshots", str(directory / "页面截图")], 90),
            ("vod", ["--probe-p7", str(directory / "vod.json")], 210),
            ("bangumi", ["--probe-p8", str(directory / "bangumi.json")], 300),
        ]
        for name, arguments, timeout in cases:
            process = subprocess.Popen([str(executable), "--data-dir", str(directory / (name + "-data")), *arguments],
                                       cwd=working, env=environment)
            print(name, "PID", process.pid, flush=True)
            code = process.wait(timeout=timeout)
            report = json.loads((directory / (name + ".json")).read_text(encoding="utf-8"))
            result["reports"][name] = {"success": report["success"], "exit_code": code,
                                       "checks": report["checks"], "file": str(directory / (name + ".json"))}
            check(name + "_passed", code == 0 and report["success"] and all(report["checks"].values()))
            check(name + "_temp_dependencies_released", not any(temporary.glob("_MEI*")))
        check("no_external_runtime_directory", not (bare / "runtime").exists())
        check("no_owned_media_cache", all(not list((directory / (name + "-data") / "cache/live").glob("*/owner.json"))
                                          for name in ("offline", "vod", "bangumi")))
        result["success"] = True
    except Exception as exc:
        result["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        if process is not None and process.poll() is None:
            close_owned_windows(executable)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                result["error"] = "Owned verification process did not exit"
        save()
    print(json.dumps({k: result.get(k) for k in ("success", "exe_sha256", "error", "normal_startup_seconds")}), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
