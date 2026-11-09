"""Terminal output: the ✓ ✗ ! · lines, a check result, desktop notifications."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from kship.check import CheckResult

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

# A message to show later: (level, text), level being one of the functions below.
Line = tuple[str, str]


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def good(msg: str) -> None:
    print(f"{_c('32', '✓')} {msg}")


def bad(msg: str) -> None:
    print(f"{_c('31', '✗')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('33', '!')} {msg}")


def note(msg: str) -> None:
    print(f"{_c('2', '·')} {msg}")


def headline(msg: str) -> None:
    print(_c("1", msg))


_LEVELS = {"good": good, "bad": bad, "warn": warn, "note": note}


def say(lines: list[Line]) -> None:
    for level, text in lines:
        _LEVELS[level](text)


def check_lines(res: CheckResult) -> list[Line]:
    lines: list[Line] = [("bad", e) for e in res.errors] + [("warn", w) for w in res.warnings]
    if res.ok:
        detail = "matches the sample submission" if res.against_sample else "passes the basic checks"
        lines.append(("good", f"{res.path.name}: {res.rows} rows, {detail}"))
    return lines


def report(res: CheckResult) -> None:
    say(check_lines(res))


def notify(title: str, text: str) -> None:
    """A macOS notification, so a long `kship run` can tell you its score while you're elsewhere."""
    if sys.platform != "darwin" or os.environ.get("KSHIP_NO_NOTIFY") or not shutil.which("osascript"):
        return
    script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
