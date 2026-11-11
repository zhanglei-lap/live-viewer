"""Try native timeshift over loopback RTMP, using only an owned private player."""
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/".research")]
from rtmp_probe_server import ProbeServer
from live_viewer.core import PlatformClient
from live_viewer.relay import LocalRelay
from live_viewer.potplayer import PotPlayerAdapter,read_ini,write_ini
from live_viewer.network import configure_session
import requests
import subprocess

sys.stdout.reconfigure(encoding="utf-8")
base=ROOT/".research/p5-rtmp-probe";base.mkdir(parents=True,exist_ok=True)
temporary=base/"video-cache";temporary.mkdir(exist_ok=True)
adapter=PotPlayerAdapter(base,Path(r"D:\potplayer\PotPlayerMini64.exe"),hidden=True)
relay=server=None
result={"success":False,"samples":[]}
actual_popen=subprocess.Popen
def private_popen(args,**kw):
    import os
    if Path(args[0]).name=="LiveViewerPot.exe":
        kw["env"]={**os.environ,"TEMP":str(temporary),"TMP":str(temporary)}
    return actual_popen(args,**kw)
try:
    adapter.prepare()
    ini=read_ini(adapter.profile.ini)
    ini["Settings"].update(UseStreamTimeShift="1",StreamTimeShiftTime="300")
    write_ini(adapter.profile.ini,ini)
    client=PlatformClient("douyu")
    stream=client.resolve("6657")
    relay=LocalRelay("douyu","6657",None,initial_client=client,initial_stream=stream)
    relay.prepare_first_source();relay.start()
    def source():
        session=configure_session(requests.Session())
        try:
            with session.get(relay.url,stream=True,timeout=(5,15)) as response:
                yield from response.iter_content(8192)
        finally:session.close()
    server=ProbeServer(source)
    address=f"rtmp://127.0.0.1:{server.port}/live/owned-probe"
    with patch("live_viewer.potplayer.valid_local_url",side_effect=lambda url:url if url==address else None), \
         patch("live_viewer.potplayer.subprocess.Popen",side_effect=private_popen):
        adapter.open(address,volume=0,mute=True)
        deadline=time.monotonic()+20
        while adapter.snapshot().get("state")!="playing" and time.monotonic()<deadline:time.sleep(.1)
        result["initial"]=adapter.snapshot()
        for _ in range(25):
            time.sleep(1)
            row=adapter.snapshot()
            if adapter.native:
                row.update(duration_seconds=adapter.native.send(0x5002)/1000,progress_seconds=adapter.native.send(0x5003)/1000)
            result["samples"].append(row)
        position=adapter.snapshot().get("position_seconds",0)
        try:
            adapter.native.send(0x5005,max(0,int((position-15)*1000)),timeout=3000)
        except Exception as exc:
            result["seek_command_error"]=getattr(exc,"code",type(exc).__name__)
        time.sleep(1)
        result["after_seek"]=adapter.snapshot()
        result["cache_files"]=[{"name":p.name,"bytes":p.stat().st_size} for p in temporary.rglob("*") if p.is_file()]
        result["success"]=result["after_seek"].get("position_seconds",position)<position-5
except Exception as exc:result["error"]=type(exc).__name__
finally:
    if server:
        server.close();result["server_error"]=server.error
        result["client_commands"]=server.commands
    if relay:relay.stop()
    adapter.close(wait=True)
    (base/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in ("samples",)},ensure_ascii=False))
    if result["samples"]:print("last",json.dumps(result["samples"][-1]))
