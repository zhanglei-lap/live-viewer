import json
from pathlib import Path
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QEventLoop,QTimer
from PySide6.QtWidgets import QApplication
from live_viewer.qt.app import ViewerWindow
from live_viewer.potplayer import PotPlayerAdapter
from live_viewer.settings import StateStore
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
def wait(ms):
    loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()
with tempfile.TemporaryDirectory(prefix="p5-trace-",dir=ROOT/".research") as data:
    store=StateStore(Path(data));config=store.load();config.update(player=r"D:\potplayer\PotPlayerMini64.exe",mute=True,volume=0)
    store.save(config);window=ViewerWindow(Path(data));window.hide()
    window.choose_room({"platform":"douyu","room":"6657","name":"trace"})
    window.runtime.player=PotPlayerAdapter(Path(data),Path(config["player"]),hidden=True)
    window.start();deadline=time.monotonic()+25
    while window.runtime.controller.state!="playing" and time.monotonic()<deadline:wait(100)
    wait(2500);serial=window.runtime.serial
    window.runtime.relay.disconnect_upstream()
    rows=[]
    for _ in range(80):
        wait(250)
        row={"native":window.runtime.player.snapshot(),"state":window.runtime.controller.state,"serial":window.runtime.serial}
        rows.append(row)
        if row["state"]=="playing" and row["serial"]!=serial and row["native"].get("position_seconds",0)>1:break
    events=[e.record() for e in window.activity.history]
    window.close()
    deadline=time.monotonic()+15
    while not window.closed and time.monotonic()<deadline:wait(50)
    result={"rows":rows,"events":events,"closed":window.closed}
    (ROOT/".research/p5-recovery-trace.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"last":rows[-1],"closed":window.closed}))
