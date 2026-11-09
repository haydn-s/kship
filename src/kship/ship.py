"""Shipping a submission: decide whether it should go, then upload it and wait for the score.

decide() runs every check (validate, dedupe, CV gate, quota) and returns a Plan: what it
concluded and the lines to show. It never uploads, so a dry run is decide() alone. The
dedupe, gate and quota rules are plain functions of their inputs (judge_*), testable
without Kaggle. upload() acts on a plan that came back "ready"; ship() runs both and
reports the outcome to the terminal and to GitHub Actions.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kship import KshipError, ledger
from kship.check import check_submission, sha256
from kship.ci import Outcome, emit
from kship.config import CONFIG_NAME, LEDGER_NAME, MARKDOWN_NAME, Config
from kship.kapi import Kaggle, Limits, explain
from kship.ui import Line, bad, check_lines, good, note, notify, rel, say, warn

# A final answer, (decision, reason), with decision one of: shipped, would-ship, skipped, blocked, failed.
Verdict = tuple[str, str]
Best = tuple[float, dict]


# -- what gets uploaded ---------------------------------------------------------


def place_sample(cfg: Config) -> tuple[Path | None, list[Line]]:
    """kship.toml's sample submission, copied into place if `kship data` unpacked it elsewhere.

    `kship init` saves the sample as data/<its name>. `kship data` keeps the archive's own
    folders (data/for_students/sample_submission.csv), so on a fresh CI runner the
    configured path can be missing even though the file was downloaded.
    """
    sample = cfg.sample_path
    if sample is None or sample.is_file():
        return sample, []
    data = cfg.root / "data"
    found = [p for p in data.rglob(sample.name) if p.is_file()] if data.is_dir() else []
    if len(found) != 1:
        return sample, []
    sample.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(found[0], sample)
    copied = f"copied {rel(found[0], cfg.root)} to {rel(sample, cfg.root)}, where kship.toml expects the sample"
    return sample, [("note", copied)]


def dir_digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(folder)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


_MERGE_LINE = re.compile(r"Merge pull request (#\d+) from \S+")


def note_line(text: str | None) -> str:
    """One line of note from free text or a whole commit message: its first non-empty line,
    except that GitHub's "Merge pull request #7 from you/branch" commits give the PR title
    (the line after it) as "<title> (#7)"."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    m = _MERGE_LINE.fullmatch(lines[0])
    if m and len(lines) > 1:
        return f"{lines[1]} ({m.group(1)})"
    return lines[0]


def build_message(
    note_text: str, cv: float | None, git: dict | None, digest: str, fallback: str, cv_scheme: str = ""
) -> str:
    """The Kaggle submission description. The tags are machine-readable on purpose:
    kship reads them back to gate and dedupe from any machine, CI included."""
    tags = [f"cv={cv:.5f}"] if cv is not None else []
    if cv is not None and cv_scheme:
        tags.append(f"cvs={cv_scheme}")
    tags += [f"git={ledger.git_label(git)}", f"sub={digest[:8]}"]
    tail = " · " + " · ".join(tags)
    head = " ".join((note_line(note_text) or fallback).split()).replace("=", ":")
    return head[: 250 - len(tail)] + tail


# -- the rules --------------------------------------------------------------------


def judge_duplicate(history: list[dict], digest: str, force: bool) -> Verdict | None:
    """Never pay twice for the same predictions."""
    dup = ledger.find_duplicate(history, digest)
    if dup is None or force:
        return None
    score = ledger.as_float(dup.get("public"))
    return (
        "skipped",
        f"identical to {ledger.label(dup)}"
        + (f" (public {score:.5f})" if score is not None else "")
        + ". Use --force to send it again.",
    )


def judge_gate(cfg: Config, cv: float | None, best_cv: Best | None, gate: str) -> tuple[Verdict | None, str]:
    """The CV gate: a verdict to stop on, or None and the line saying why it passed ("" if no gate).

    Without a CV there's nothing to beat, and a pipeline that stopped printing one (say it
    moved to `logging`, which writes to stderr) would otherwise spend a submission on every merge.
    """
    if gate != "better":
        return None, ""
    scheme = f" (cv_scheme {cfg.cv_scheme!r})" if cfg.cv_scheme else ""
    if cv is None:
        reason = (
            "no CV score to gate on. Print KSHIP_CV=<score> to stdout or pass --cv, "
            "or use --gate always to ship without one."
        )
        return ("skipped", reason), ""
    higher = cfg.higher_is_better
    if higher is None:
        reason = "set higher_is_better in kship.toml so the CV gate knows which way is up (or pass --gate always)"
        return ("failed", reason), ""
    if best_cv is None:
        return None, f"cv {cv:.5f} is the first CV on record{scheme}"
    best, record = best_cv
    if not ledger.improves(cv, best, higher, cfg.min_improvement):
        reason = (
            f"cv {cv:.5f} doesn't beat your best {best:.5f}{scheme} "
            f"({ledger.label(record)}). Use --gate always to ship anyway."
        )
        return ("skipped", reason), ""
    return None, f"cv {cv:.5f} beats your best {best:.5f}{scheme} ({ledger.label(record)})"


def judge_quota(cfg: Config, lim: Limits | None, automated: bool) -> Verdict | None:
    """Respect the daily quota, keeping `reserve` slots back when automation is driving."""
    if lim is None:
        return None
    slug = cfg.competition
    floor = cfg.reserve if automated else 0
    if lim.allowed_now == 0 and lim.total == 0:
        return (
            "failed",
            f"Kaggle shows no submissions available for {slug}. If you haven't "
            f"joined, accept the rules: https://www.kaggle.com/competitions/{slug}/rules",
        )
    if lim.allowed_now == 0:
        return "blocked", f"daily limit reached ({lim.today} used today); it resets at 00:00 UTC"
    if lim.allowed_now <= floor:
        return (
            "blocked",
            f"{lim.allowed_now} submission(s) left today and reserve = {floor}, so "
            "automation won't spend them. Ship it with `kship submit`, or re-run "
            "after 00:00 UTC.",
        )
    return None


# -- deciding -----------------------------------------------------------------------


@dataclass
class Plan:
    """What decide() concluded. Final unless decision is "ready" (cleared to upload)."""

    decision: str
    reason: str
    outcome: Outcome
    lines: list[Line] = field(default_factory=list)  # to show, in order
    digest: str = ""
    git: dict | None = None
    kaggle: Kaggle | None = None
    limits: Limits | None = None
    best_public: Best | None = None


def decide(
    cfg: Config,
    path: Path,
    *,
    connect: Callable[[], Kaggle],
    cv: float | None = None,
    note_text: str = "",
    gate: str = "always",
    automated: bool = False,
    force: bool = False,
    dry_run: bool = False,
    lines: list[Line] | None = None,
) -> Plan:
    """Every check short of uploading. `lines`, if given, collects the lines to show as they come."""
    slug = cfg.competition
    out = Outcome(decision="failed", reason="", competition=slug, cv=cv, file=rel(path, cfg.root))
    plan = Plan("ready", "", out, lines=lines if lines is not None else [])

    def stop(decision: str, reason: str) -> Plan:
        plan.decision, plan.reason = decision, reason
        return plan

    # 1. Validate what's about to be uploaded.
    if cfg.mode == "notebook":
        if not (path / "kernel-metadata.json").is_file():
            return stop("failed", f"{rel(path, cfg.root)}/kernel-metadata.json is missing")
        plan.digest = dir_digest(path)
        plan.lines.append(("good", f"kernel {rel(path, cfg.root)}/ ready to push"))
        ready = f"{rel(path, cfg.root)}/ is ready to push"
    else:
        sample, placed = place_sample(cfg)
        plan.lines += placed
        if sample is not None and not sample.is_file():
            return stop(
                "failed",
                f"the sample submission ({rel(sample, cfg.root)}) is missing, so the IDs can't be checked. "
                f"Run `kship data`, or fix sample_submission in {CONFIG_NAME}",
            )
        res = check_submission(path, sample, cfg.value_range)
        plan.lines += check_lines(res)
        if not res.ok:
            return stop("failed", "fix the file first; uploading it would have cost a submission")
        plan.digest = sha256(path)
        checked = "passes every check" if res.against_sample else "passes the basic checks (no sample to compare)"
        ready = f"{rel(path, cfg.root)} {checked}"

    # 2. History = this machine's ledger + everything Kaggle has on record. Reading Kaggle
    #    means a fresh CI runner (or another laptop) gates against the same best score.
    remote: list[dict] = []
    try:
        plan.kaggle = connect()
        remote = [ledger.from_kaggle(s) for s in plan.kaggle.all_submissions(slug)]
    except Exception as e:  # no credentials, 403 before the rules are accepted, network
        msg = str(e).splitlines()[0] if isinstance(e, KshipError) else explain(e)
        if not dry_run:
            return stop(
                "failed",
                f"can't read your submissions from Kaggle: {msg} "
                f"(rules: https://www.kaggle.com/competitions/{slug}/rules)",
            )
        plan.lines.append(("warn", f"can't reach Kaggle ({msg}); judging against the local ledger only"))
        plan.kaggle = None
    history = ledger.merge(ledger.load(cfg.ledger_path), remote)

    higher = cfg.higher_is_better
    # CVs only compare within one cv_scheme; public scores always compare.
    comparable = ledger.same_scheme(history, cfg.cv_scheme)
    best_cv = ledger.best(comparable, "cv", higher) if higher is not None else None
    plan.best_public = ledger.best(history, "public", higher if higher is not None else True)
    out.best_cv = best_cv[0] if best_cv else None
    out.best_public = plan.best_public[0] if plan.best_public else None

    # 3. Dedupe, then 4. the CV gate.
    if verdict := judge_duplicate(history, plan.digest, force):
        return stop(*verdict)
    gate_verdict, passed = judge_gate(cfg, cv, best_cv, gate)
    if gate_verdict:
        return stop(*gate_verdict)
    if passed and best_cv:
        plan.lines.append(("good", passed))

    plan.git = ledger.git_info(cfg.root, ignore=(LEDGER_NAME, MARKDOWN_NAME))
    fallback = path.name if cfg.mode == "file" else "kernel"
    out.message = build_message(note_text, cv, plan.git, plan.digest, fallback, cfg.cv_scheme)
    if plan.git and plan.git.get("dirty"):
        plan.lines.append(("warn", "uncommitted changes: this submission won't be reproducible from its commit alone"))

    # 5. The daily quota. A dry run stops before it: it says whether this would ship, not when.
    plan.limits = plan.kaggle.limits(slug) if plan.kaggle else None
    if plan.limits is not None:
        out.left_today = plan.limits.allowed_now
    if dry_run:
        return stop("would-ship", passed or ready)
    if verdict := judge_quota(cfg, plan.limits, automated):
        return stop(*verdict)
    return plan


# -- uploading ----------------------------------------------------------------------


def upload(
    cfg: Config,
    path: Path,
    plan: Plan,
    *,
    note_text: str = "",
    cv: float | None = None,
    automated: bool = False,
    wait: bool = True,
    command: str = "",
) -> Verdict:
    """Act on a "ready" plan: upload, record it in the ledger, wait for the leaderboard score."""
    k = plan.kaggle
    assert plan.decision == "ready" and k is not None  # decide() only says "ready" once it has read Kaggle
    slug, out = cfg.competition, plan.outcome
    message = out.message

    # 6. Upload.
    kernel_version = None
    try:
        if cfg.mode == "notebook":
            kernel_id, kernel_version = k.push_kernel(path)
            good(f"pushed {kernel_id} version {kernel_version}; waiting for it to run on Kaggle")
            k.wait_kernel(
                kernel_id,
                cfg.kernel_timeout_minutes * 60,
                on_tick=lambda s, t: note(f"kernel {s.lower()} ({t / 60:.0f} min)"),
            )
            ref = k.submit_kernel(slug, kernel_id, kernel_version, cfg.kernel_output, message)
        else:
            ref = k.submit_file(slug, path, message)
    except Exception as e:
        return "failed", f"upload failed: {e if isinstance(e, KshipError) else explain(e)}"
    good(f"submitted to {slug}: {message}")

    rec = ledger.append(
        cfg.ledger_path,
        {
            "time": ledger.now(),
            "file": rel(path, cfg.root),
            "sha256": plan.digest,
            "sub": plan.digest[:8],
            "git": plan.git,
            "cv": cv,
            "cv_scheme": cfg.cv_scheme or None,
            "note": note_line(note_text),
            "message": message,
            "ref": ref,
            "mode": cfg.mode,
            "kernel_version": kernel_version,
            "command": command or None,
            "automated": automated,
            "ci": bool(os.environ.get("GITHUB_ACTIONS")),
            "status": "pending",
            "public": None,
            "private": None,
        },
    )
    if plan.limits is not None:
        out.left_today = max(plan.limits.allowed_now - 1, 0)

    if not wait:
        return "shipped", "submitted; not waiting for the score (`kship log --refresh` picks it up)"

    # 7. Wait for the leaderboard and record what came back.
    note(f"waiting up to {cfg.poll_minutes:g} min for the score...")
    try:
        sub = k.wait_for_score(slug, ref, message, cfg.poll_minutes * 60)
    except Exception as e:
        warn(f"lost track of the score ({e})")
        sub = None
    records = ledger.load(cfg.ledger_path)
    # Not by "n": a union-merged ledger can hold two records with the same number.
    current = next(r for r in records if (r.get("time"), r.get("sha256")) == (rec["time"], rec["sha256"]))
    if sub is not None:
        current["ref"] = current.get("ref") or sub.ref
        current["status"] = ledger.KAGGLE_STATUS.get(sub.status, "pending")
        current["public"], current["private"] = sub.public, sub.private
        if sub.error:
            current["error"] = sub.error
    ledger.save(cfg.ledger_path, records)

    if current["status"] == "error":
        notify("kship: submission failed", current.get("error") or slug)
        return "failed", f"Kaggle couldn't score it: {current.get('error') or 'no details'}"
    if current["public"] is None:
        return "shipped", "submitted; still scoring, run `kship log --refresh` in a bit"

    public = out.public = current["public"]
    line = f"public score {public:.5f}"
    if plan.best_public:
        best = plan.best_public[0]
        delta = public - best
        direction = cfg.higher_is_better if cfg.higher_is_better is not None else True
        line += f" ({'+' if delta >= 0 else ''}{delta:.5f} vs your best {best:.5f}"
        line += ", new best!)" if ledger.improves(public, best, direction) else ")"
    notify(f"kship: {slug}", line)
    return "shipped", line


# -- both, reported -----------------------------------------------------------------

_ANNOUNCE: dict[str, Callable[[str], None]] = {
    "shipped": good,
    "would-ship": lambda m: good(f"dry run, nothing uploaded: would ship. {m}"),
    "skipped": lambda m: note(f"not shipped: {m}"),
    "blocked": lambda m: warn(f"not shipped: {m}"),
    "failed": bad,
}


def finish(out: Outcome, decision: str, reason: str) -> int:
    out.decision, out.reason = decision, reason
    _ANNOUNCE[decision](reason)
    emit(out)
    return out.code


def ship(
    cfg: Config,
    path: Path,
    *,
    connect: Callable[[], Kaggle],
    note_text: str = "",
    cv: float | None = None,
    gate: str = "always",
    automated: bool = False,
    force: bool = False,
    dry_run: bool = False,
    wait: bool = True,
    command: str = "",
) -> int:
    """Validate, dedupe, gate, check quota, upload, wait for the score. Returns an exit code."""
    lines: list[Line] = []
    try:
        plan = decide(
            cfg,
            path,
            connect=connect,
            cv=cv,
            note_text=note_text,
            gate=gate,
            automated=automated,
            force=force,
            dry_run=dry_run,
            lines=lines,
        )
    finally:
        say(lines)  # even if deciding blew up halfway, show what it found
    if plan.decision != "ready":
        return finish(plan.outcome, plan.decision, plan.reason)
    verdict = upload(cfg, path, plan, note_text=note_text, cv=cv, automated=automated, wait=wait, command=command)
    return finish(plan.outcome, *verdict)
