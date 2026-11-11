"""Inspect only the owned private player's preference controls and resulting keys."""
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import sys
import time
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live_viewer.potplayer import PotPlayerAdapter, read_ini, write_ini
from live_viewer.events import safe_text
from potplayer_probe_support import original_settings_fingerprint

vsr = "--vsr" in sys.argv
base = ROOT / (".research/p5-vsr-discovery" if vsr else ".research/p5-settings-discovery")
base.mkdir(parents=True, exist_ok=True)
adapter = PotPlayerAdapter(base, Path(r"D:\potplayer\PotPlayerMini64.exe"), hidden=True)
before = original_settings_fingerprint()
report = {"pages": [], "success": False}
try:
    adapter.prepare()
    ini = read_ini(adapter.profile.ini)
    ini["Settings"]["Language"] = "English.ini"
    write_ini(adapter.profile.ini, ini)
    adapter.open("http://127.0.0.1:9/p5-probe/live.flv", volume=0, mute=True)
    deadline = time.monotonic() + 10
    while (adapter.native is None or not adapter.native.find()) and time.monotonic() < deadline:
        time.sleep(.1)
    native = adapter.native
    user = native.user
    user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
    user.GetDlgCtrlID.argtypes = [W.HWND]
    user.EnumChildWindows.argtypes = [W.HWND, native.enum, W.LPARAM]
    def send(window, message, wp=0, lp=0):
        pid = W.DWORD()
        user.GetWindowThreadProcessId(window, C.byref(pid))
        if pid.value != adapter.process.pid or not native.owns():
            raise RuntimeError("Not our player window")
        value = C.c_size_t()
        if not user.SendMessageTimeoutW(window, message, wp, lp, 0x22, 1000, C.byref(value)):
            raise RuntimeError("Owned window timeout")
        return C.c_ssize_t(value.value).value
    def windows(parent=None):
        found = []
        @native.enum
        def visit(window, _):
            pid = W.DWORD()
            user.GetWindowThreadProcessId(window, C.byref(pid))
            if pid.value == adapter.process.pid:
                cls, title = C.create_unicode_buffer(256), C.create_unicode_buffer(2048)
                user.GetClassNameW(window, cls, 256)
                user.GetWindowTextW(window, title, 2048)
                if cls.value in ("Edit", "ComboBox"):
                    text_buffer = C.create_unicode_buffer(2048)
                    try:
                        send(window, 0xD, 2048, C.addressof(text_buffer))
                        title.value = text_buffer.value
                    except RuntimeError:
                        pass
                found.append({"handle": window, "class": cls.value, "id": user.GetDlgCtrlID(window),
                              "text": safe_text(title.value), "visible": bool(user.IsWindowVisible(window))})
            return True
        (user.EnumChildWindows(parent, visit, 0) if parent else user.EnumWindows(visit, 0))
        return found
    native.send(10010, message=0x111, timeout=3000)
    time.sleep(.8)
    dialogs = [w for w in windows() if w["class"] == "#32770" and w["visible"]]
    if not dialogs:
        raise RuntimeError("Preferences dialog missing")
    dialog = dialogs[0]["handle"]
    children = windows(dialog)
    tree = next(w["handle"] for w in children if w["class"] == "SysTreeView32")
    root = send(tree, 0x110A, 0, 0)  # TVM_GETNEXTITEM / TVGN_ROOT
    nodes = []
    def collect(item):
        while item:
            nodes.append(item)
            child = send(tree, 0x110A, 4, item)
            if child:
                collect(child)
            item = send(tree, 0x110A, 1, item)
    collect(root)
    for index, item in enumerate(nodes):
        send(tree, 0x110B, 9, item)  # TVM_SELECTITEM / TVGN_CARET
        time.sleep(.06)
        controls = [w for w in windows(dialog) if w["visible"] and w["class"] != "SysTreeView32"]
        page = {"index": index, "controls": [{k: v for k, v in w.items() if k not in ("handle", "visible")} for w in controls]}
        report["pages"].append(page)
        if vsr and any(w["id"] == 3055 and "D3D11" in w["text"] for w in controls):
            report["vsr_controls"] = page["controls"]
            report["comboboxes"] = {}
            for control in controls:
                if control["class"] == "ComboBox":
                    handle = control["handle"]
                    choices = []
                    for index in range(max(0, send(handle, 0x146))):
                        buffer = C.create_unicode_buffer(1024)
                        send(handle, 0x148, index, C.addressof(buffer))
                        choices.append(buffer.value)
                    report["comboboxes"][str(control["id"])] = choices
                    if any("Direct3D 11" in item for item in choices):
                        selected = next(i for i, item in enumerate(choices) if "Direct3D 11" in item)
                        send(handle, 0x14E, selected)
                        parent = user.GetParent(handle)
                        send(parent, 0x111, control["id"] | (1 << 16), handle)
            sr = next(w for w in windows(dialog) if w["id"] == 3055 and w["visible"])
            if not send(sr["handle"], 0xF0):
                send(sr["handle"], 0xF5)
            send(next(w["handle"] for w in windows(dialog) if w["id"] == 3043 and w["visible"]), 0xF5)
            send(next(w["handle"] for w in windows(dialog) if w["id"] == 2), 0xF5)
            report["vsr_page"] = index
            break
        if not vsr and any(w["id"] == 3293 and "(mms," in w["text"] for w in controls):
            shift = next(w for w in controls if w["id"] == 3040 and w["class"] == "Button")
            report["timeshift_page"] = index
            report["timeshift_before"] = send(shift["handle"], 0xF0)
            send(shift["handle"], 0xF5)  # BM_CLICK
            report["timeshift_after"] = send(shift["handle"], 0xF0)
            report["timeshift_controls"] = [{k: v for k, v in w.items() if k not in ("handle", "visible")} for w in controls]
            time_edit = next(w for w in controls if w["id"] == 3267 and w["class"] == "Edit")
            value = C.create_unicode_buffer("1440")
            send(time_edit["handle"], 0xC, 0, C.addressof(value))
            if not report["timeshift_after"]:
                send(shift["handle"], 0xF5)
            send(next(w["handle"] for w in windows(dialog) if w["id"] == 3043 and w["visible"]), 0xF5)
            send(next(w["handle"] for w in windows(dialog) if w["id"] == 2), 0xF5)
            break
    time.sleep(.3)
    adapter.close(wait=True)
    report["private_ini"] = {section: dict(read_ini(base / "potplayer/preferences.ini")[section])
                             for section in read_ini(base / "potplayer/preferences.ini").sections() if section == "Settings"}
    report["original_settings_unchanged"] = original_settings_fingerprint() == before
    report["success"] = ("vsr_page" in report if vsr else "timeshift_page" in report) and report["original_settings_unchanged"]
except Exception as exc:
    report["error"] = type(exc).__name__ + ": " + str(exc)
finally:
    adapter.close(wait=True)
    (base / "inspection.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": report["success"], "timeshift_page": report.get("timeshift_page"),
                      "error": report.get("error"), "pages": len(report["pages"]), "settings": report.get("private_ini")}, ensure_ascii=False))
