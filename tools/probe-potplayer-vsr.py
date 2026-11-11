"""Visible owned-player VSR A/B matrix with locally generated test charts."""
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import subprocess
import sys
import time
from PIL import ImageGrab

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from live_viewer.potplayer import PrivateProfile,Win32Player,read_ini,write_ini
from live_viewer.resources import CacheLease,process_memory
from potplayer_probe_support import original_settings_fingerprint
sys.stdout.reconfigure(encoding="utf-8")
base=ROOT/".research/p5-vsr-matrix";base.mkdir(parents=True,exist_ok=True)
result={"success":False,"matrix":[],"original_settings_unchanged":False,"activation_verified":False}
before=original_settings_fingerprint()
for height,fps in ((720,30),(720,60),(1080,30),(1080,60)):
    for enabled in (False,True):
        profile=PrivateProfile(base,Path(r"D:\potplayer\PotPlayerMini64.exe"))
        profile.prepare()
        ini=read_ini(profile.ini)
        ini["Settings"].update(VideoRen2="15",Direct3D11Mode="1",UseD3D11VSR="1" if enabled else "0",
                               UseD3D11RTXVideoHDR="0",AudioVolume="0",AudioMute="1")
        write_ini(profile.ini,ini)
        cache=CacheLease(base)
        path=ROOT/f".research/p5-fixtures/chart-{height}p-{fps}.mp4"
        startup=subprocess.STARTUPINFO();startup.dwFlags=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=4
        process=subprocess.Popen([str(profile.executable),"/new",str(path)],cwd=profile.path,startupinfo=startup,
                                 creationflags=subprocess.CREATE_NO_WINDOW,env=cache.environment())
        native=Win32Player(process,profile.executable)
        row={"source_height":height,"source_fps":fps,"vsr_requested":enabled}
        try:
            deadline=time.monotonic()+12
            while not native.find() and time.monotonic()<deadline:time.sleep(.1)
            snapshot=None
            while time.monotonic()<deadline:
                try:
                    snapshot=native.snapshot(str(path))
                    if snapshot["width"]>0 and snapshot["position_seconds"]>.1:break
                except Exception:pass
                time.sleep(.1)
            if not snapshot or snapshot["width"]==0:raise RuntimeError("No native video")
            native.caption("P5 VSR "+str(height)+"p"+str(fps)+" "+("ON" if enabled else "OFF"))
            native.send(10013,message=0x111,timeout=3000)
            time.sleep(1.2)
            row["gpu"]=subprocess.run(["nvidia-smi","--query-gpu=utilization.gpu,memory.used,power.draw",
                                      "--format=csv,noheader,nounits"],capture_output=True,text=True,timeout=8,
                                     creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
            row["native"]=native.snapshot(str(path));row.update(process_memory(process))
            native.send(0x5007,1,timeout=3000)
            native.send(0x5005,1000,timeout=3000)
            time.sleep(.35)
            capture=base/f"{height}p-{fps}-{'on' if enabled else 'off'}.png"
            ImageGrab.grab(window=native.hwnd).save(capture)
            row["screenshot"]=capture.name
            row["paused_position"]=native.send(0x5004)/1000
            # Ask this owned player for its information panel and capture text only.
            native.user.PostMessageW(native.hwnd,0x111,10195,0)
            time.sleep(.3)
            windows=[]
            @native.enum
            def visit(window,_):
                pid=W.DWORD();native.user.GetWindowThreadProcessId(window,C.byref(pid))
                if pid.value==process.pid:
                    cls=C.create_unicode_buffer(256)
                    native.user.GetClassNameW(window,cls,256)
                    if cls.value=="#32770" and native.user.IsWindowVisible(window):windows.append(window)
                return True
            native.user.EnumWindows(visit,0)
            if windows:
                texts=[]
                @native.enum
                def child(window,_):
                    buffer=C.create_unicode_buffer(8192);returned=C.c_size_t()
                    native.user.SendMessageTimeoutW(window,0xD,8192,C.addressof(buffer),0x22,500,C.byref(returned))
                    if buffer.value:texts.append(buffer.value)
                    return True
                native.user.EnumChildWindows.argtypes=[W.HWND,native.enum,W.LPARAM]
                native.user.EnumChildWindows(windows[0],child,0)
                row["renderer_info"]=[line for text in texts for line in text.splitlines()
                                      if any(k in line.lower() for k in ("renderer","d3d11","super","vsr","渲染","帧率","丢弃"))]
                native.user.PostMessageW(windows[0],0x10,0,0)
            row["success"]=True
        except Exception as exc:row["error"]=type(exc).__name__
        finally:
            native.close()
            try:process.wait(3)
            except subprocess.TimeoutExpired:
                if native.owns():process.terminate();process.wait(3)
            native.close_receiver()
            if process.poll() is not None:cache.close()
            profile.save_and_clean()
            result["matrix"].append(row)
            print(json.dumps({k:v for k,v in row.items() if k not in ("native","renderer_info")}),flush=True)
result["original_settings_unchanged"]=original_settings_fingerprint()==before
result["success"]=all(row.get("success") for row in result["matrix"]) and result["original_settings_unchanged"]
(base/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
