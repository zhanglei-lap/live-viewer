"""Run isolated native Qt checks at the three agreed DPI scales."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
from PySide6.QtWidgets import QApplication
application = QApplication([])
base_ratio = application.primaryScreen().devicePixelRatio()
result = {"success": False, "runs": []}
for scale in ("1.25", "1.5", "2"):
    output = ROOT / ".research" / ("p5-ui-" + scale + ".json")
    screenshots = ROOT / ".research" / ("p5-ui-" + scale)
    with tempfile.TemporaryDirectory(prefix="p5-ui-", dir=ROOT / ".research") as temporary:
        env = dict(os.environ, QT_SCALE_FACTOR=str(float(scale) / base_ratio))
        process = subprocess.run([sys.executable, str(ROOT / "qt_main.py"), "--data-dir", temporary,
                                  "--validate", str(output), "--screenshots", str(screenshots)],
                                 cwd=ROOT, env=env, timeout=45)
    report = json.loads(output.read_text(encoding="utf-8"))
    result["runs"].append({"scale": scale, "exit_code": process.returncode, "report": report})
result["success"] = all(run["exit_code"] == 0 and run["report"]["success"] for run in result["runs"])
(ROOT / ".research/p5-dpi-validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"success": result["success"],
                  "runs": [{"scale": run["scale"], "success": run["report"]["success"],
                            "ratio": run["report"]["device_pixel_ratio"],
                            "error": run["report"].get("error")} for run in result["runs"]]}))
raise SystemExit(0 if result["success"] else 1)


