"""Run with the project's Qt environment; defaults to an independent visible UI."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live_viewer.bangumi_validation import evaluate_gate, inspect_matrix, run_native
from live_viewer.settings import atomic_write


def main():
    parser = argparse.ArgumentParser(description="P8-A independent Bangumi validation")
    parser.add_argument("--anonymous", action="store_true", help="No UI and no account access")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data-p8a")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--player", default=r"D:\potplayer\PotPlayerMini64.exe")
    parser.add_argument("--native-seconds", type=int, default=0)
    parser.add_argument("--full-episode", action="store_true")
    parser.add_argument("--verify-eos", action="store_true", help="After the short sample, seek to EOS and check a fresh owned instance")
    parser.add_argument("--show-login", action="store_true", help="Open the visible QR dialog")
    parser.add_argument("--auto-member", action="store_true", help="After the user confirms QR login, run the six member samples")
    parser.add_argument("--proxy-configured", action="store_true", help="Set an unreachable proxy in THIS process only")
    args = parser.parse_args()
    if not 0 <= args.native_seconds <= 1800:
        parser.error("native-seconds must be in 0..1800")
    args.data_dir = args.data_dir.resolve()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.report = args.report or args.data_dir / "p8a-report.json"
    if args.proxy_configured:
        os.environ.update(HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9", NO_PROXY="")
    if not args.anonymous:
        from live_viewer.qt.bangumi_validation import launch
        return launch(args)
    def save(report):
        evaluate_gate(report)
        atomic_write(args.report, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    report = inspect_matrix(on_row=lambda r: print("EP", r["episode"]["ep_id"], r["status"], flush=True))
    save(report)
    if args.native_seconds or args.full_episode or args.verify_eos:
        first = True
        for row in report["samples"]:
            if row["status"] != "pgc.whole" or not row.get("identity_matches_sample"):
                continue
            row["native"] = run_native(f"ep{row['episode']['ep_id']}", None, args.data_dir, args.player,
                duration=args.native_seconds or 300, full_episode=args.full_episode and first,
                verify_eos=args.verify_eos, hidden=True,
                on_progress=lambda msg: print(msg.encode('ascii', errors='backslashreplace').decode(), flush=True))
            first = False
            if args.proxy_configured and row["native"]["status"] == "passed":
                report["d2_actual_api_and_cdn_passed"] = True
            save(report)
            print("native", row["episode"]["ep_id"], row["native"]["status"], flush=True)
    save(report)
    # Permissions denied / preview are valid anonymous findings, not gate success.
    return int(any((r.get("native") or {}).get("status") not in (None, "passed") for r in report["samples"]))


if __name__ == "__main__":
    raise SystemExit(main())
