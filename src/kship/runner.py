"""Running the pipeline for `kship run`: mirror its output live, collect its KSHIP_* markers."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from kship import KshipError

# Markers have to be on stdout, one per line; the last of each kind wins.
_MARKER = re.compile(rb"^\s*KSHIP_(CV|FILE|MSG)=(.*?)\s*$")


def run_streaming(cmd: list[str], env: dict[str, str]) -> tuple[int, dict[str, str]]:
    """Run cmd, mirroring its stdout live, and collect KSHIP_* marker lines it prints."""
    markers: dict[str, str] = {}

    def scan(line: bytes) -> None:
        m = _MARKER.match(line)
        if m:
            markers[m.group(1).decode().lower()] = m.group(2).decode(errors="replace")

    # Piped (CI logs), stdout is block-buffered; flush so our lines come before the pipeline's.
    sys.stdout.flush()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, env=env)
    except FileNotFoundError:
        raise KshipError(f"command not found: {cmd[0]}") from None
    assert proc.stdout is not None  # stdout=PIPE
    out = getattr(sys.stdout, "buffer", None)
    tail = b""
    fd = proc.stdout.fileno()
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        if out is not None:
            out.write(chunk)
        else:
            sys.stdout.write(chunk.decode(errors="replace"))
        sys.stdout.flush()
        *lines, tail = re.split(rb"[\r\n]", tail + chunk)
        for line in lines:
            scan(line)
        if len(tail) > 1 << 20:
            tail = tail[-4096:]
    scan(tail)
    return proc.wait(), markers


def newest_csv(folder: Path, since: float | None = None) -> Path | None:
    if not folder.is_dir():
        return None
    files = [p for p in folder.iterdir() if p.is_file() and p.name.endswith((".csv", ".csv.gz", ".zip"))]
    if since is not None:
        files = [p for p in files if p.stat().st_mtime >= since]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)
