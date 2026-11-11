"""Refresh final documentation without changing the already validated executable."""
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
folder = ROOT / "dist/p6/LiveViewer-0.7.0"
archive = folder.parent / "LiveViewer-0.7.0-win-x64.zip"
receipt = ROOT / ".research/p6-package-artifacts.json"
record = json.loads(receipt.read_text(encoding="utf-8"))
exe = folder / "LiveViewer-0.7.0.exe"
assert record["success"] and record["exe_sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()
for source, target in (("docs/p6-usage.md", "使用说明.md"), ("docs/p6-acceptance.md", "P6-验收.md"), ("docs/p5-usage.md", "p5-usage.md")):
    shutil.copyfile(ROOT / source, folder / target)
    shutil.copyfile(ROOT / source, folder / "source" / source)
shutil.copyfile(ROOT / "tools/build-p6.py", folder / "source/tools/build-p6.py")
assets = [exe, folder / "使用说明.md", folder / "P6-验收.md", folder / "Runtime-Notes.txt", folder / "p5-usage.md"]
for sub in ("runtime", "licenses", "source"):
    assets.extend(p for p in (folder / sub).rglob("*") if p.is_file())
temporary = archive.with_suffix(".zip.tmp")
with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
    for file in sorted(assets):
        package.write(file, file.relative_to(folder.parent).as_posix())
with zipfile.ZipFile(temporary) as package:
    assert package.testzip() is None
    prefix = folder.name + "/"
    assert hashlib.sha256(package.read(prefix + exe.name)).hexdigest() == record["exe_sha256"]
    assert not any(name.removeprefix(prefix).lower().startswith(("data/", ".local/", "credentials/", "logs/"))
                   or name.lower().endswith((".dpapi", ".ini", ".dpl")) for name in package.namelist())
    assert all(package.read(prefix + "source/" + file.relative_to(ROOT).as_posix()) == file.read_bytes()
               for file in (ROOT / "live_viewer").rglob("*") if file.is_file() and file.suffix in (".py", ".qss", ".lua"))
temporary.replace(archive)
record["bytes"] = archive.stat().st_size
record["zip_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
record["documentation_finalized"] = True
receipt.write_text(json.dumps(record, indent=2), encoding="utf-8")
print(json.dumps({"success": True, "zip": str(archive), "bytes": record["bytes"], "sha256": record["zip_sha256"]}))
