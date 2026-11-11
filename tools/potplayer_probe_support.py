"""Read-only comparison of the original player's settings; never exports values."""
import hashlib
import os


def original_settings_fingerprint():
    if os.name != "nt":
        return None
    import winreg
    values = []
    def visit(key, prefix):
        count = winreg.QueryInfoKey(key)
        for index in range(count[1]):
            name, value, kind = winreg.EnumValue(key, index)
            values.append((prefix, name, repr((value, kind))))
        for index in range(count[0]):
            name = winreg.EnumKey(key, index)
            with winreg.OpenKey(key, name) as child:
                visit(child, prefix + "/" + name)
    for path in (r"Software\DAUM\PotPlayerMini64", r"Software\DAUM\PotPlayer64"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                visit(key, path)
        except FileNotFoundError:
            pass
    return hashlib.sha256(repr(sorted(values)).encode("utf-8", errors="replace")).hexdigest()
