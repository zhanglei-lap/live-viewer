"""Build P1 in its own output directory without replacing previous releases."""
from pathlib import Path
import os
import runpy

ROOT = Path(__file__).resolve().parent.parent
os.environ["LIVE_VIEWER_DIST_SUBDIR"] = "p1"
runpy.run_path(str(ROOT / "tools" / "build-windows.py"), run_name="__main__")
