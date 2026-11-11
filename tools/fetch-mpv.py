"""Fetch the pinned first-party Windows LGPL library, verifying both digests."""
import hashlib
import json
from pathlib import Path
import zipfile

import requests

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = ROOT / "vendor" / "libmpv"
ASSET = "libmpv-v0.41.0-dev-geb0ee1031-37471512912-x86_64-w64-mingw32-lgpl.zip"
URL = "https://github.com/mpv-player/mpv/releases/download/git-release/" + ASSET
ARCHIVE_SHA = "25a6421b677e97160f89af4f1bde4d515e54721d7a18d2ac30c406dcf7085f86"
DLL_SHA = "1a782e67d5deb4af26ecd858ea2048efb3985b9069391cdd343fd799fea0a2b5"


def main():
    dll = DIRECTORY / "libmpv-2.dll"
    if dll.is_file() and hashlib.sha256(dll.read_bytes()).hexdigest() == DLL_SHA:
        print("Pinned libmpv already verified.")
        return
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    archive_path = DIRECTORY / "download.zip"
    with requests.get(URL, stream=True, timeout=(8, 30)) as response:
        response.raise_for_status()
        with archive_path.open("wb") as file:
            for block in response.iter_content(1024 * 1024):
                file.write(block)
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != ARCHIVE_SHA:
        raise RuntimeError("The official archive digest did not match.")
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            target = (DIRECTORY / item.filename).resolve()
            if not target.is_relative_to(DIRECTORY.resolve()):
                raise RuntimeError("Archive path escaped the vendor directory.")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))
    if hashlib.sha256(dll.read_bytes()).hexdigest() != DLL_SHA:
        raise RuntimeError("The library digest did not match.")
    (DIRECTORY / "source.json").write_text(json.dumps({"repository": "mpv-player/mpv", "release_tag": "git-release",
        "asset": ASSET, "download_url": URL, "archive_sha256": ARCHIVE_SHA, "dll_sha256": DLL_SHA,
        "license_variant": "LGPL"}, indent=2), encoding="utf-8")
    print("Pinned libmpv downloaded and verified.")


if __name__ == "__main__": main()
