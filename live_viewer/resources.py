"""Own-process resource readings and strictly owned native temporary directories."""
import ctypes
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import shutil
import uuid
from .settings import atomic_write


def available_memory():
    if os.name != "nt": return {}
    class Memory(ctypes.Structure):
        _fields_ = [("length", W.DWORD), ("load", W.DWORD)] + [(name, ctypes.c_ulonglong) for name in
                   ("total_physical", "free_physical", "total_commit", "free_commit", "total_virtual", "free_virtual", "reserved")]
    value = Memory();value.length = ctypes.sizeof(value)
    api = ctypes.WinDLL("kernel32")
    if not api.GlobalMemoryStatusEx(ctypes.byref(value)): return {}
    return {"free_physical_mb": round(value.free_physical / 1048576),
            "free_commit_mb": round(value.free_commit / 1048576)}


def process_memory(process):
    if os.name != "nt" or process is None or process.poll() is not None or not hasattr(process, "_handle"): return {}
    class Counters(ctypes.Structure):
        _fields_ = [("size", W.DWORD), ("faults", W.DWORD)] + [(name, ctypes.c_size_t) for name in
                   ("peak_working", "working", "peak_pool_paged", "pool_paged", "peak_pool_nonpaged",
                    "pool_nonpaged", "pagefile", "peak_pagefile", "private")]
    value = Counters();value.size = ctypes.sizeof(value)
    api = ctypes.WinDLL("psapi")
    api.GetProcessMemoryInfo.argtypes = [W.HANDLE, ctypes.c_void_p, W.DWORD]
    if not api.GetProcessMemoryInfo(W.HANDLE(int(process._handle)), ctypes.byref(value), value.size): return {}
    return {"working_set_mb": round(value.working / 1048576, 1), "private_memory_mb": round(value.private / 1048576, 1)}


class CacheLease:
    def __init__(self, directory):
        self.root = Path(directory).resolve() / "cache" / "live"
        self.identity = uuid.uuid4().hex
        self.path = self.root / self.identity
        self.path.mkdir(parents=True, exist_ok=False)
        atomic_write(self.path / "owner.json", json.dumps({"native_temp_cache": 1, "identity": self.identity}).encode())
        self.closed = False

    def environment(self):
        return {**os.environ, "TEMP": str(self.path), "TMP": str(self.path)}

    def close(self):
        if self.closed: return
        resolved = self.path.resolve()
        if resolved.parent != self.root.resolve() or self.path.is_symlink() or self.path.is_junction():
            raise ValueError("Cache cleanup escaped its owned directory")
        owner = json.loads((self.path / "owner.json").read_text())
        if owner.get("identity") != self.identity or owner.get("native_temp_cache") != 1:
            raise ValueError("Cache ownership mismatch")
        shutil.rmtree(resolved)
        self.closed = True
