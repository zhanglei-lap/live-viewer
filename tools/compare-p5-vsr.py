"""Measure controlled paused-chart pixel differences, excluding system overlays."""
import json
from pathlib import Path
from PIL import Image,ImageChops,ImageStat
ROOT=Path(__file__).resolve().parents[1]
base=ROOT/".research/p5-vsr-matrix"
result=json.loads((base/"result.json").read_text(encoding="utf-8"))
differences=[]
for height,fps in ((720,30),(720,60),(1080,30),(1080,60)):
    off=Image.open(base/f"{height}p-{fps}-off.png")
    on=Image.open(base/f"{height}p-{fps}-on.png")
    region=(130,220,off.width-130,off.height-430)
    delta=ImageChops.difference(off.crop(region),on.crop(region))
    means=ImageStat.Stat(delta).mean
    differences.append({"height":height,"fps":fps,"output_size":[off.width,off.height],
                        "mean_channel_difference":means,"same_paused_position":all(
                            r["paused_position"]==1 for r in result["matrix"] if r["source_height"]==height and r["source_fps"]==fps)})
result["controlled_image_differences"]=differences
result["activation_verified"]=all(d["same_paused_position"] and max(d["mean_channel_difference"])>.1 for d in differences)
result["activation_evidence"]="Owned D3D11 renderer / NVIDIA DXVA, same-frame image output changes and paired GPU measurements"
(base/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({"activation_verified":result["activation_verified"],"differences":differences}),flush=True)
