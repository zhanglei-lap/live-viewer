"""Opt-in build-only workaround for an unresponsive Windows WMI service."""
import os

if os.name == "nt" and os.environ.get("LIVE_VIEWER_BUILD_NO_WMI") == "1":
    import platform
    def unavailable_wmi(*args, **kwargs):
        # Python's platform module falls back to native/registry version data.
        raise OSError("WMI is disabled only for this application build.")
    platform._wmi_query = unavailable_wmi
