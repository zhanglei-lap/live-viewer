"""Generate local moving charts for a controlled 720p/1080p 30/60fps matrix."""
from fractions import Fraction
from pathlib import Path
import sys
import av
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1]
folder=ROOT/".research/p5-fixtures";folder.mkdir(parents=True,exist_ok=True)
for height,fps in ((720,30),(720,60),(1080,30),(1080,60)):
    path=folder/f"chart-{height}p-{fps}.mp4"
    if path.exists():continue
    width=height*16//9
    image=Image.new("RGB",(width,height),"#606060")
    draw=ImageDraw.Draw(image)
    font=ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf",28)
    draw.text((30,25),f"LiveViewer P5 test chart  {width}x{height}  {fps} fps",font=font,fill="white")
    for x in range(20,width-20,9):
        draw.line((x,100,x+80,height//2),fill="white",width=1)
    for i in range(6):
        draw.text((30, height//2+i*30),f"Fine detail sample {i} ABC abc 0123456789",font=font,fill=(255-i*30,)*3)
    output=av.open(str(path),"w")
    video=output.add_stream("libx264",rate=fps)
    video.width=width;video.height=height;video.pix_fmt="yuv420p"
    video.options={"preset":"ultrafast","crf":"28","threads":"2"}
    for index in range(fps*8):
        frame_image=image.copy()
        moving=ImageDraw.Draw(frame_image)
        x=(index*13)%(width-120)
        moving.rectangle((x,height-100,x+90,height-30),fill="#14d6bc")
        frame=av.VideoFrame(width,height,"rgb24")
        packed=frame_image.tobytes()
        stride=frame.planes[0].line_size
        if stride!=width*3:
            packed=b"".join(packed[y*width*3:(y+1)*width*3]+b"\0"*(stride-width*3) for y in range(height))
        frame.planes[0].update(packed)
        frame.pts=index;frame.time_base=Fraction(1,fps)
        for packet in video.encode(frame):output.mux(packet)
    for packet in video.encode():output.mux(packet)
    output.close()
    print(path.name, path.stat().st_size,flush=True)
