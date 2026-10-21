"""Collect ecobee cpss journal logs over ADB or SSH.

Opens a live `journalctl -u ecobee.cpss.service -f` stream on the device,
tees every line to an on-disk log file as it arrives, and yields the same
lines so a caller can parse them after the fact. Parsing is intentionally
not done here -- this module only knows how to fetch and persist raw lines.
"""

import subprocess
from pathlib import Path
from typing import Iterator, Optional

JOURNAL_CMD = ["journalctl", "-u", "ecobee.cpss.service", "-f"]


def _build_command(adb_serial: Optional[str], ssh_target: Optional[str]) -> list[str]:
    if ssh_target is not None:
        return ["ssh", ssh_target, " ".join(JOURNAL_CMD)]
    cmd = ["adb"]
    if adb_serial:
        cmd += ["-s", adb_serial]
    cmd += ["shell"] + JOURNAL_CMD
    return cmd


def stream_logs(
    outfile: Path,
    adb_serial: Optional[str] = None,
    ssh_target: Optional[str] = None,
) -> Iterator[str]:
    """Tail the device journal, tee each line to outfile, and yield it.

    Exactly one transport is used: pass ssh_target ("user@host") for SSH,
    otherwise ADB is used (adb_serial may be None/empty to pick the sole
    attached device). Runs until the subprocess ends or the caller stops
    iterating (e.g. on Ctrl+C or a duration timeout), at which point the
    subprocess is terminated.
    """
    command = _build_command(adb_serial, ssh_target)
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        with outfile.open("a", buffering=1) as fh:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                fh.write(line + "\n")
                yield line
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
