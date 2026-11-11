"""Opt-in diagnostics for an explicitly requested packaged self-test."""
import sys

if "--self-test" in sys.argv:
    import faulthandler
    from pathlib import Path
    index = sys.argv.index("--self-test")
    if index + 1 < len(sys.argv):
        target = Path(sys.argv[index + 1]).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        _trace_file = target.with_suffix(".trace.txt").open("w", encoding="utf-8")
        faulthandler.enable(_trace_file)
        faulthandler.dump_traceback_later(20, repeat=False, file=_trace_file)
