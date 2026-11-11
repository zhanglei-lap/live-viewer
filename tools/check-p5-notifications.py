"""Observe OS tray callback, retained unread alert and optional owned fullscreen."""
import json
from pathlib import Path
import sys
import tempfile
import time
import subprocess
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QEventLoop,QTimer
from PySide6.QtWidgets import QApplication
from live_viewer.qt.app import ViewerWindow
from live_viewer.qt.notifications import notification_state
from live_viewer.potplayer import PrivateProfile, Win32Player, read_ini, write_ini

app=QApplication([]);app.setQuitOnLastWindowClosed(False)
result={"success":False,"os_state":notification_state(),"checks":{}}
def wait(ms):
    loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()
with tempfile.TemporaryDirectory(prefix="p5-notice-",dir=ROOT/".research") as data:
    window=ViewerWindow(Path(data));window.hide()
    native=process=profile=None
    if "--fullscreen" in sys.argv:
        profile=PrivateProfile(Path(data),Path(r"D:\potplayer\PotPlayerMini64.exe"));profile.prepare()
        ini=read_ini(profile.ini);ini["Settings"].update(AudioVolume="0",AudioMute="1")
        write_ini(profile.ini,ini)
        sample=ROOT/".research/p5-fixtures/chart-720p-30.mp4"
        process=subprocess.Popen([str(profile.executable),"/new",str(sample)],cwd=profile.path,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        native=Win32Player(process,profile.executable)
        deadline=time.monotonic()+10
        while not native.find() and time.monotonic()<deadline:wait(100)
        native.send(10013,message=0x111,timeout=3000)
        wait(300)
        result["fullscreen_owned_player"]=True
        result["os_state"]=notification_state()
    window.activity.emit("test.notice_requested","本地直播：后台通知验证，不影响实际播放。",source="app",
                         level="ERROR",presentation="strong",impact="degraded",scope="test")
    window.tick()
    result["unread_immediately_after_request"]=len(window.notices.unread)
    wait(6000)
    result["requests"]=window.notices.requests
    result["os_display_callbacks"]=window.notices.os_shown
    was_read=any(e.code=="notice.read" for e in window.activity.history)
    result["checks"]["unread_retained_in_background"]=result["unread_immediately_after_request"]==1 and (
        len(window.notices.unread)==1 or was_read)
    result["read_acknowledged"]=was_read
    result["checks"]["persistent_fault_retained"]=window.activity.notice_for("test") is not None
    result["checks"]["no_background_modal_window"]=window.strong_dialog is None
    result["checks"]["os_display_observed"]=window.notices.os_shown>0
    window.notices.open();wait(300)
    result["checks"]["opening_retains_fault_and_acknowledges_unread"]=not window.notices.unread and window.notice_event is not None
    window.close()
    deadline=time.monotonic()+5
    while not window.closed and time.monotonic()<deadline:wait(50)
    result["success"]=all(result["checks"].values())
    if native:
        native.close()
        try:process.wait(3)
        except subprocess.TimeoutExpired:
            if native.owns():process.terminate();process.wait(3)
        native.close_receiver();profile.save_and_clean()
(ROOT/".research/p5-notifications-validation.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(result),flush=True)
