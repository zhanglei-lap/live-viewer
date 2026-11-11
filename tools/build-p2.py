"""Build the PotPlayer edition; installed PotPlayer binaries are never bundled."""
from pathlib import Path
import os
import runpy

ROOT = Path(__file__).resolve().parent.parent
os.environ["LIVE_VIEWER_DIST_SUBDIR"] = "p2"
os.environ["LIVE_VIEWER_INCLUDE_MPV"] = "0"
runpy.run_path(str(ROOT / "tools/build-windows.py"), run_name="__main__")
