"""CLI entry point: collect ecobee cpss logs, then summarize equipment engagement.

Usage:
    python cli.py --adb [SERIAL] [--outfile PATH] [--duration SECONDS]
    python cli.py --ssh user@host [--outfile PATH] [--duration SECONDS]

Streams `journalctl -u ecobee.cpss.service -f` from the device over ADB or
SSH, tees the raw lines to --outfile while live, then (after the stream
ends via Ctrl+C or --duration) parses the saved log and prints an
equipment-engagement timeline + totals to the console.
"""

import argparse
import threading
from datetime import datetime
from pathlib import Path

from collector import stream_logs
from parser import parse_line
from summarizer import format_report, summarize


LOG_DIR = Path("logs")


def _default_outfile() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOG_DIR / f"ecobee_cpss_{ts}.log"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collect ecobee cpss logs and summarize equipment engagement."
    )
    transport = p.add_mutually_exclusive_group(required=True)
    transport.add_argument(
        "--adb", nargs="?", const="", metavar="SERIAL",
        help="Collect over ADB (optionally specify device SERIAL)",
    )
    transport.add_argument(
        "--ssh", metavar="USER@HOST",
        help="Collect over SSH",
    )
    p.add_argument(
        "--outfile", type=Path, default=None,
        help="Path to save raw log lines (default: timestamped file under ./logs)",
    )
    p.add_argument(
        "--duration", type=float, default=None,
        help="Stop collecting after this many seconds (default: run until Ctrl+C)",
    )
    return p.parse_args(argv)


def collect(args: argparse.Namespace) -> Path:
    outfile = args.outfile or _default_outfile()
    outfile.parent.mkdir(parents=True, exist_ok=True)
    adb_serial = args.adb if args.adb else None
    ssh_target = args.ssh

    stop_event = threading.Event()
    timer = None
    if args.duration is not None:
        timer = threading.Timer(args.duration, stop_event.set)
        timer.start()

    print(f"Collecting ecobee cpss logs -> {outfile}")
    print("Press Ctrl+C to stop." if args.duration is None
          else f"Collecting for {args.duration}s...")

    gen = stream_logs(outfile, adb_serial=adb_serial, ssh_target=ssh_target)
    try:
        for _ in gen:
            if stop_event.is_set():
                break
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        gen.close()
        if timer is not None:
            timer.cancel()

    return outfile


def summarize_file(outfile: Path) -> str:
    events = []
    with outfile.open() as fh:
        for line in fh:
            event = parse_line(line.rstrip("\n"))
            if event is not None:
                events.append(event)
    result = summarize(events)
    return format_report(result)


def main(argv=None) -> None:
    args = parse_args(argv)
    outfile = collect(args)
    print()
    print(summarize_file(outfile))


if __name__ == "__main__":
    main()
