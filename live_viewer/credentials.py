from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

import requests

from .core import COOKIE_DOMAINS, ViewerError, belongs_to
from .settings import atomic_write


def _dpapi(payload: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise ViewerError("记住登录目前仅支持 Windows。")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                         ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    # UI_FORBIDDEN = 1; LOCAL_MACHINE is intentionally not set (per-user scope).
    if not function(ctypes.byref(source), None if decrypt else "LiveViewer login", None, None, None, 1, ctypes.byref(target)):
        raise ViewerError("Windows 无法解密保存的登录，请重新导入。" if decrypt else "Windows 登录加密失败，本次登录仅保留在内存中。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class CredentialStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory) / "credentials"

    def _path(self, platform):
        if platform not in COOKIE_DOMAINS:
            raise ViewerError("不支持的平台。")
        return self.directory / (platform + ".dpapi")

    def save(self, platform, jar):
        records = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                    "secure": c.secure, "expires": c.expires, "rest": dict(c._rest)}
                   for c in jar if belongs_to(c.domain, COOKIE_DOMAINS[platform]) and not c.is_expired()]
        if not records:
            raise ViewerError("没有可保存的有效 Cookie，请重新导入。")
        encrypted = _dpapi(json.dumps({"platform": platform, "cookies": records}, ensure_ascii=False).encode("utf-8"))
        try:
            atomic_write(self._path(platform), encrypted)
        except OSError:
            raise ViewerError("登录保存失败，请将程序放在可写目录；本次登录仍可使用。") from None

    def load(self, platform):
        path = self._path(platform)
        if not path.exists():
            return None
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError()
            data = json.loads(_dpapi(path.read_bytes(), decrypt=True))
            if data.get("platform") != platform:
                raise ValueError()
            jar = requests.cookies.RequestsCookieJar()
            for c in data["cookies"]:
                if belongs_to(c["domain"], COOKIE_DOMAINS[platform]) and isinstance(c["name"], str) and isinstance(c["value"], str):
                    jar.set(c["name"], c["value"], domain=c["domain"], path=c.get("path") or "/",
                            secure=bool(c.get("secure", True)), expires=c.get("expires"), rest=c.get("rest", {}))
            for cookie in list(jar):
                if cookie.is_expired():
                    jar.clear(cookie.domain, cookie.path, cookie.name)
            if not jar:
                raise ViewerError("记住的登录已过期，请重新导入。")
            return jar
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ViewerError("保存的登录无法读取，请重新导入。") from None

    def delete(self, platform):
        try:
            self._path(platform).unlink(missing_ok=True)
        except OSError:
            raise ViewerError("无法删除保存的登录，请检查数据目录权限。") from None
