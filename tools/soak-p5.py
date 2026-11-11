"""Thirty-minute real Qt/native replay/VSR soak, with owned resource cleanup."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QEventLoop,QTimer
from PySide6.QtWidgets import QApplication
from live_viewer.qt.app import ViewerWindow
from live_viewer.settings import StateStore
from live_viewer.potplayer import PotPlayerAdapter
from live_viewer.resources import available_memory
from potplayer_probe_support import original_settings_fingerprint

parser=argparse.ArgumentParser();parser.add_argument("--minutes",type=float,default=30)
parser.add_argument("--output",type=Path,default=ROOT/".research/p5-soak-validation.json")
args=parser.parse_args()
output=args.output
report={"success":False,"minutes_requested":args.minutes,"checks":{},"samples":[]}
baseline=original_settings_fingerprint()
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
def wait(ms):
    loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()
def pump(fn,timeout=60):
    limit=time.monotonic()+timeout
    while time.monotonic()<limit:
        wait(50)
        if fn():return True
    return False
def check(name,value):
    report["checks"][name]=bool(value)
    print(name,str(bool(value)),flush=True)
    if not value:raise RuntimeError(name)
with tempfile.TemporaryDirectory(prefix="p5-soak-",dir=ROOT/".research") as temporary:
    store=StateStore(Path(temporary));config=store.load()
    config.update(player=r"D:\potplayer\PotPlayerMini64.exe",volume=0,mute=True,native_replay=True,replay_minutes=10,vsr=True)
    store.save(config)
    window=ViewerWindow(store.directory);window.hide()
    window.choose_room({"platform":"douyu","room":"6657","name":"P5 30-minute native replay test"})
    window.runtime.player=PotPlayerAdapter(store.directory,Path(config["player"]),hidden=False)
    adapter=window.runtime.player
    beats=[];beat=QTimer();beat.timeout.connect(lambda:beats.append(time.monotonic()));beat.start(100)
    def playing():
        data=window.runtime.player.snapshot()
        return (window.runtime.controller.state=="playing" and data.get("owned_source") and
                data.get("width",0)>0 and (data.get("position_seconds") or 0)>1)
    try:
        window.start()
        check("initial_native_replay_playing",pump(playing))
        # Keep visible output larger than 1080p without owning the whole screen.
        import ctypes as C
        from ctypes import wintypes as W
        adapter.native.user.SetWindowPos.argtypes=[W.HWND,W.HWND,C.c_int,C.c_int,C.c_int,C.c_int,W.UINT]
        adapter.native.user.SetWindowPos(adapter.native.hwnd,None,0,0,2100,1250,0x14)
        started=time.monotonic();last=started-30
        milestones=set();original_serial=window.runtime.serial
        while time.monotonic()-started<args.minutes*60:
            wait(100)
            elapsed=time.monotonic()-started
            if elapsed-last+started>=30:
                data=window.runtime.player.snapshot()
                sample={"elapsed":round(elapsed,1),"player":data,"playback_state":window.runtime.controller.state,
                        "relay":window.runtime.relay.snapshot() if window.runtime.relay else {},"memory":available_memory()}
                report["samples"].append(sample)
                output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
                print("sample",round(elapsed),"state",data.get("state"),"position",data.get("position_seconds"),
                      "duration",data.get("duration_seconds"),"private_mb",data.get("private_memory_mb"),flush=True)
                last=time.monotonic()
            if elapsed>40 and "seek" not in milestones:
                adapter=window.runtime.player
                position=adapter.snapshot()["position_seconds"]
                adapter.seek(max(0,position-20))
                check("native_seek_reaches_cached_history",pump(lambda:
                    adapter.snapshot().get("position_seconds",position)<position-8,10))
                check("seek_keeps_same_player_item",window.runtime.serial==original_serial)
                milestones.add("seek")
            if elapsed>70 and "pause" not in milestones:
                window.pause();check("paused_from_qt",pump(lambda:adapter.snapshot().get("state")=="paused",10))
                before_pause=adapter.snapshot()["position_seconds"]
                wait(4000)
                window.pause()
                check("continue_uses_same_media",pump(playing,10) and window.runtime.serial==original_serial)
                check("continue_preserves_replay_position",adapter.snapshot()["position_seconds"]<before_pause+10)
                window.reconnect()
                check("live_edge_preserves_cache",pump(playing,10) and window.runtime.serial==original_serial)
                milestones.add("pause")
            if elapsed>120 and "recovery" not in milestones:
                serial=window.runtime.serial
                window.runtime.relay.disconnect_upstream()
                check("controlled_disconnect_recovers",pump(lambda:window.runtime.serial!=serial and playing(),75))
                adapter=window.runtime.player
                original_serial=window.runtime.serial
                milestones.add("recovery")
        report["elapsed_seconds"]=round(time.monotonic()-started,1)
        check("at_least_requested_watch_duration",report["elapsed_seconds"]>=args.minutes*60)
        check("qt_remains_responsive",bool(beats) and max(b-a for a,b in zip(beats,beats[1:]))<2)
        check("native_media_confirmations_absent",all(s["player"].get("visible_dialogs",0)==0 for s in report["samples"]))
        window.stop()
        adapter=window.runtime.player
        check("stop_delivered",pump(lambda:adapter.snapshot().get("state")=="closed_media",10))
        window.close();check("app_shutdown_completed",pump(lambda:window.closed,15))
        check("owned_player_process_exited",adapter.process is None or adapter.process.poll() is not None)
        check("owned_temp_video_cache_removed",not any((store.directory/"cache/live").glob("*/owner.json")))
        check("original_player_preferences_unchanged",original_settings_fingerprint()==baseline)
        report["success"]=True
    except Exception as exc:
        report["error"]=str(exc)
    finally:
        beat.stop()
        if not window.closed:window.close();pump(lambda:window.closed,20)
        report["events"]=[e.record() for e in window.activity.history]
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print("P5 soak completed",report["success"],flush=True)
raise SystemExit(0 if report["success"] else 1)
