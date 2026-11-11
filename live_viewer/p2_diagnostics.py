"""Offline release checks: no actual PotPlayer invocation or user profile reads."""
from pathlib import Path

import requests

from .core import PlatformClient, Stream, ViewerError
from .potplayer import PrivateProfile, read_ini, valid_local_url
from .relay import LocalRelay, RelayEndpoint


def check_p2(directory):
    directory = Path(directory) / "p2-offline"
    installed = directory / "synthetic-install"
    installed.mkdir(parents=True)
    exe = bytearray(256)
    exe[:2] = b"MZ"
    exe[0x3C:0x40] = (128).to_bytes(4, "little")
    exe[128:134] = b"PE\0\0\x64\x86"
    path = installed / "PotPlayerMini64.exe"
    path.write_bytes(exe)
    (installed / "PotPlayer64.dll").write_bytes(b"SYNTHETIC_DO_NOT_EXECUTE")
    (installed / "PotPlayerMini64.ini").write_text("SYNTHETIC_ORIGINAL")
    profile = PrivateProfile(directory / "data", path)
    checks = {}
    endpoint = RelayEndpoint()
    relays = []
    try:
        profile.prepare()
        checks["private_executable_and_ini"] = profile.executable.name == "LiveViewerPot.exe" and profile.ini.is_file()
        checks["original_settings_untouched"] = (installed / "PotPlayerMini64.ini").read_text() == "SYNTHETIC_ORIGINAL"
        checks["error_windows_disabled"] = read_ini(profile.ini)["Settings"]["IgnoreErrorMsg"] == "1"
        try:
            valid_local_url("https://example.invalid/live.flv")
        except ViewerError:
            checks["external_media_rejected"] = True
        class Response:
            def __init__(self, data):
                self.data = data
            def close(self):
                pass
        url = None
        session = requests.Session();session.trust_env = False
        try:
            for payload in (b"FLV-FIRST", b"FLV-SECOND"):
                client = PlatformClient("douyu")
                stream = Stream("douyu", "6657", 0, 0, "模拟", [], [])
                relay = LocalRelay("douyu", "6657", None, initial_client=client, initial_stream=stream,
                                   endpoint=endpoint, low_latency=False)
                relays.append(relay)
                relay.initial_media = (Response(payload), iter(()), payload)
                relay.start()
                url = url or relay.url
                response = session.get(relay.url, timeout=3)
                checks["stable_listener"] = url == relay.url and response.content == payload
                relay.stop()
            checks["stopped_listener_unavailable"] = session.get(url, timeout=3).status_code == 503
        finally:
            session.close()
    finally:
        for relay in relays:
            relay.stop()
        endpoint.close()
        run = profile.path
        profile.save_and_clean()
        checks["owned_profile_cleaned"] = run is not None and not run.exists() and path.exists()
    return {"success": all(checks.values()), "checks": checks, "player_launched": False}
