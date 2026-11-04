"""The submission ledger: one JSON line per submission, committed with your code.

Every line ties a leaderboard score back to the exact commit, file hash and CV
score that produced it, so any past submission can be reproduced or explained.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path


def git_info(cwd: Path) -> dict | None:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    try:
        sha = git("rev-parse", "--short", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
    except subprocess.CalledProcessError:
        dirty, branch = False, ""
    return {"sha": sha, "dirty": dirty, "branch": branch}


def git_label(info: dict | None) -> str:
    if not info:
        return "no-git"
    return info["sha"] + ("*" if info.get("dirty") else "")


def load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def save(path: Path, records: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    tmp.replace(path)


def append(path: Path, record: dict) -> dict:
    records = load(path)
    record = {"n": (records[-1]["n"] + 1) if records else 1, **record}
    with open(path, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def as_float(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def counted(records: list[dict]) -> list[dict]:
    """Submissions that reached Kaggle and weren't rejected."""
    return [r for r in records if r.get("status") not in ("error", "failed")]


def best(records: list[dict], key: str, higher: bool) -> tuple[float, dict] | None:
    scored = [(as_float(r.get(key)), r) for r in counted(records)]
    scored = [(v, r) for v, r in scored if v is not None]
    if not scored:
        return None
    return (max if higher else min)(scored, key=lambda t: t[0])


def improves(new: float, old: float, higher: bool, margin: float = 0.0) -> bool:
    return new > old + margin if higher else new < old - margin


def find_duplicate(records: list[dict], digest: str) -> dict | None:
    for r in reversed(counted(records)):
        if r.get("sha256") == digest or r.get("sub") == digest[:8]:
            return r
    return None


def label(r: dict) -> str:
    return f"#{r['n']}" if r.get("n") else f"Kaggle submission {r.get('ref')}"


# -- Kaggle as the source of truth ---------------------------------------------
# kship tags every submission message with `cv=`, `git=` and `sub=` (the file's
# fingerprint). Reading those back from Kaggle lets a fresh CI runner, or another
# machine, gate and dedupe against everything ever submitted, with no shared state.

_TAG = re.compile(r"(?<![\w=])(cv|git|sub)=([^\s·]+)")
KAGGLE_STATUS = {"COMPLETE": "scored", "ERROR": "error"}


def parse_message(desc: str) -> dict:
    out: dict = {}
    for key, val in _TAG.findall(desc or ""):
        if key == "cv":
            out["cv"] = as_float(val)
        elif key == "git":
            out["git"] = {"sha": val.rstrip("*"), "dirty": val.endswith("*"), "branch": ""}
        elif key == "sub":
            out["sub"] = val
    first = (desc or "").split(" · ")[0]
    if first and not _TAG.match(first):
        out["note"] = first
    return out


def from_kaggle(sub) -> dict:
    rec = {
        "ref": sub.ref,
        "time": sub.date,
        "message": sub.description,
        "status": KAGGLE_STATUS.get(sub.status, "pending"),
        "public": sub.public,
        "private": sub.private,
        "source": "kaggle",
    }
    rec.update(parse_message(sub.description))
    return rec


def merge(local: list[dict], remote: list[dict]) -> list[dict]:
    """Local records plus any Kaggle submissions they don't already cover."""
    known = {str(r.get("ref")) for r in local if r.get("ref")}
    return local + [r for r in remote if str(r.get("ref")) not in known]


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


def cv_lb_verdict(records: list[dict]) -> str | None:
    """How far your local CV can be trusted, judged by how it ranks against the public LB."""
    pairs = [(as_float(r.get("cv")), as_float(r.get("public"))) for r in counted(records)]
    pairs = [(c, p) for c, p in pairs if c is not None and p is not None]
    if len(pairs) < 4:
        return None
    rho = spearman([c for c, _ in pairs], [p for _, p in pairs])
    if rho is None:
        return None
    if rho >= 0.7:
        verdict = "your CV ranks submissions the way the leaderboard does. Trust it."
    elif rho >= 0.3:
        verdict = "CV and leaderboard only loosely agree. Lean on CV, but watch the gap."
    else:
        verdict = (
            "CV and leaderboard disagree. Either the public split is small and noisy, or "
            "your CV leaks. Don't chase the public score until you know which."
        )
    return f"CV↔LB rank correlation over {len(pairs)} submissions: ρ = {rho:+.2f}. {verdict}"


def _fmt(x, digits: int = 5) -> str:
    v = as_float(x)
    return "—" if v is None else f"{v:.{digits}f}"


def rows(records: list[dict]) -> list[list[str]]:
    out = []
    for r in records:
        out.append(
            [
                str(r.get("n", "")),
                (r.get("time") or "")[:16].replace("T", " "),
                git_label(r.get("git")),
                _fmt(r.get("cv")),
                _fmt(r.get("public")),
                r.get("status", ""),
                r.get("note", ""),
            ]
        )
    return out


HEADERS = ["#", "when", "commit", "cv", "public", "status", "note"]


def render_table(records: list[dict]) -> str:
    data = [HEADERS] + rows(records)
    widths = [max(len(row[i]) for row in data) for i in range(len(HEADERS))]
    widths[-1] = min(widths[-1], 48)
    lines = []
    for k, row in enumerate(data):
        cells = [row[i][: widths[i]].ljust(widths[i]) for i in range(len(row))]
        lines.append("  ".join(cells).rstrip())
        if k == 0:
            lines.append("  ".join("─" * w for w in widths))
    return "\n".join(lines)


def render_markdown(records: list[dict], competition: str) -> str:
    md = [
        "# Submission history",
        "",
        f"Generated by `kship log --md` for [{competition}](https://www.kaggle.com/competitions/{competition}). "
        "Public scores are what the leaderboard reported; private scores appear after the deadline.",
        "",
        "| " + " | ".join(HEADERS) + " | private |",
        "|" + "---|" * (len(HEADERS) + 1),
    ]
    for r, row in zip(records, rows(records), strict=True):
        row[-1] = row[-1].replace("|", "\\|")
        md.append("| " + " | ".join(row) + f" | {_fmt(r.get('private'))} |")
    verdict = cv_lb_verdict(records)
    if verdict:
        md += ["", verdict]
    return "\n".join(md) + "\n"
