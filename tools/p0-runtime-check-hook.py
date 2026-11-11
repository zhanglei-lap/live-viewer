"""Trace only an explicitly requested prototype self-test, never normal use."""
import sys
if "--self-test" in sys.argv:
    import faulthandler
    from pathlib import Path
    index = sys.argv.index("--self-test")
    if index + 1 < len(sys.argv):
        output = Path(sys.argv[index + 1]).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        _p0_trace = output.with_suffix(".trace.txt").open("w", encoding="utf-8")
        faulthandler.enable(_p0_trace)
        faulthandler.dump_traceback_later(15, file=_p0_trace)
