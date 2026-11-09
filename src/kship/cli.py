"""kship command line: doctor, new, init, data, check, submit, run, log."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

from kship import KshipError, __version__, ledger
from kship.check import CheckResult, check_submission, sha256
from kship.ci import EXIT_FAILED, Outcome, emit, in_actions, write_workflow
from kship.config import CONFIG_NAME, Config, find_config, guess_higher_is_better, load, render
from kship.kapi import AUTH_HELP, Kaggle, credential_sources, explain
from kship.scaffold import write_kernel, write_project

FALLBACK_REPO = "https://github.com/haydn-s/kship"


def default_source() -> str:
    """Where generated CI workflows install kship from.

    KSHIP_SOURCE wins. Otherwise, if this copy was installed straight from git, reuse
    exactly that (PEP 610 direct_url.json). Otherwise use the Repository URL in
    pyproject.toml, pinned to this version's tag.
    """
    if env := os.environ.get("KSHIP_SOURCE"):
        return env
    try:
        dist = metadata.distribution("kship")
    except metadata.PackageNotFoundError:
        return f"git+{FALLBACK_REPO}@v{__version__}"
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        direct = {}
    vcs = direct.get("vcs_info") or {}
    if vcs.get("vcs") == "git" and direct.get("url"):
        rev = vcs.get("requested_revision") or vcs.get("commit_id")
        return f"git+{direct['url']}" + (f"@{rev}" if rev else "")
    for entry in dist.metadata.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("repository", "source", "homepage"):
            return f"git+{url.strip().removesuffix('.git')}@v{__version__}"
    return f"git+{FALLBACK_REPO}@v{__version__}"


# Tests swap this for a fake.
make_kaggle = Kaggle

# -- output -------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


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


def notify(title: str, text: str) -> None:
    """A macOS notification, so a long `kship run` can tell you its score while you're elsewhere."""
    if sys.platform != "darwin" or os.environ.get("KSHIP_NO_NOTIFY") or not shutil.which("osascript"):
        return
    script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


def report(res: CheckResult) -> None:
    for e in res.errors:
        bad(e)
    for w in res.warnings:
        warn(w)
    if res.ok:
        detail = "matches the sample submission" if res.against_sample else "passes the basic checks"
        good(f"{res.path.name}: {res.rows} rows, {detail}")


# -- helpers ------------------------------------------------------------------


def newest_csv(folder: Path, since: float | None = None) -> Path | None:
    if not folder.is_dir():
        return None
    files = [p for p in folder.iterdir() if p.is_file() and p.name.endswith((".csv", ".csv.gz", ".zip"))]
    if since is not None:
        files = [p for p in files if p.stat().st_mtime >= since]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def resolve_file(arg: str | None, cfg: Config) -> Path:
    if arg:
        return Path(arg).resolve()
    found = newest_csv(cfg.submissions_path)
    if found is None:
        raise KshipError(f"no file given and nothing in {cfg.submission_dir}/ to pick up")
    return found


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


def build_message(note_text: str, cv: float | None, git: dict | None, digest: str, fallback: str) -> str:
    """The Kaggle submission description. The tags are machine-readable on purpose:
    kship reads them back to gate and dedupe from any machine, CI included."""
    tags = ([f"cv={cv:.5f}"] if cv is not None else []) + [f"git={ledger.git_label(git)}", f"sub={digest[:8]}"]
    tail = " · " + " · ".join(tags)
    head = " ".join((note_line(note_text) or fallback).split()).replace("=", ":")
    return head[: 250 - len(tail)] + tail


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


# -- doctor -------------------------------------------------------------------


def competition_report(k: Kaggle, slug: str) -> bool:
    ok = True
    info = k.competition(slug)
    if info is None:
        warn(
            f"{slug} doesn't show up in Kaggle's search (normal for private class competitions); "
            "submitting can still work"
        )
    else:
        good(f"{info.title}  ({info.url})")
        facts = [
            f
            for f in (
                f"deadline {info.deadline}" if info.deadline else "",
                f"metric {info.metric}" if info.metric else "",
                f"{info.max_daily}/day" if info.max_daily else "",
                "code competition (notebook submissions)" if info.code_only else "",
            )
            if f
        ]
        if facts:
            note(", ".join(facts))
        if info.closed:
            warn("this competition isn't accepting submissions")
        if info.joined is False:
            bad(f"you haven't joined it yet. Accept the rules first: {info.url}/rules")
            ok = False
    lim = k.limits(slug)
    if lim is not None and (lim.allowed_now or lim.total):
        good(f"{lim.allowed_now} submissions left today ({lim.today} used, {lim.total} total). Resets 00:00 UTC.")
    return ok


def cmd_doctor(args) -> int:
    ok = True
    headline("kship doctor")
    try:
        kv = metadata.version("kaggle")
        good(f"kaggle package {kv} (Python {sys.version.split()[0]})")
    except metadata.PackageNotFoundError:
        bad("the kaggle package isn't installed in kship's environment")
        return 1

    cli = shutil.which("kaggle")
    if cli:
        good(f"kaggle CLI on PATH: {cli}")
        if "/.venv/" in cli or "/.VENV/" in cli or "/venv/" in cli:
            warn("that CLI lives inside a project virtualenv; install it globally with `uv tool install kaggle`")
    else:
        warn("no `kaggle` command on PATH (kship doesn't need it, but it's handy): uv tool install kaggle")

    sources = credential_sources()
    if not sources:
        bad("no Kaggle credentials anywhere")
        if in_actions():
            print(
                "Add a repository secret named KAGGLE_API_TOKEN (a KGAT_... token from "
                "https://www.kaggle.com/settings/api):\n  gh secret set KAGGLE_API_TOKEN"
            )
        else:
            print(AUTH_HELP)
        return 1
    for s in sources:
        good(f"credentials: {s.label}" + (f" ({s.path})" if s.path else ""))
        if s.loose_permissions:
            warn(f"{s.path} is readable by other users; fix with: chmod 600 {s.path}")
    if len(sources) > 1:
        note(f"Kaggle uses the first one that works: {sources[0].label}")

    try:
        k = make_kaggle()
        user, method = k.whoami()
        good(f"signed in as {user}" + (f" via {method}" if method else ""))
    except KshipError as e:
        bad("the credentials didn't work")
        print(e)
        return 1

    slug = args.competition
    if not slug and find_config():
        slug = load().competition
    if slug:
        ok = competition_report(k, slug) and ok
    else:
        note("run inside a competition project (or pass -c <slug>) to check a competition too")
    return 0 if ok else 1


# -- init / new / data --------------------------------------------------------


def _pick_sample(names: list[str]) -> str | None:
    pats = [r"sample.*submission.*\.csv", r"submission.*\.csv", r"sample.*\.csv"]
    for pat in pats:
        for n in names:
            if re.search(pat, Path(n).name, re.I):
                return n
    return None


def init_project(root: Path, slug: str, force: bool = False) -> Config:
    cfg_path = root / CONFIG_NAME
    if cfg_path.exists() and not force:
        raise KshipError(f"{cfg_path} already exists (use --force to overwrite)")
    root.mkdir(parents=True, exist_ok=True)

    info, sample_rel, user = None, "", None
    try:
        k = make_kaggle()
        user, _ = k.whoami()
        info = k.competition(slug)
        try:
            name = _pick_sample(k.files(slug))
            if name:
                sample_rel = rel(k.download_file(slug, name, root / "data"), root)
                good(f"downloaded {sample_rel}")
            else:
                warn("no sample submission among the competition files; set sample_submission yourself")
        except Exception as e:  # 403 before the rules are accepted, network, ...
            msg = str(e) if isinstance(e, KshipError) else explain(e)
            warn(f"couldn't fetch the sample submission ({msg}); set sample_submission in {CONFIG_NAME} yourself")
    except KshipError as e:
        warn(f"working offline: {e}".splitlines()[0])

    metric = info.metric if info else ""
    cfg = Config(
        competition=slug,
        root=root,
        mode="notebook" if info and info.code_only else "file",
        sample_submission=sample_rel,
        metric=metric,
        higher_is_better=guess_higher_is_better(metric),
    )
    cfg_path.write_text(
        render(
            cfg,
            title=info.title if info else "",
            deadline=info.deadline if info else "",
            daily=info.max_daily if info else None,
        )
    )
    good(f"wrote {rel(cfg_path, Path.cwd())}")
    if metric:
        direction = "higher" if cfg.higher_is_better else "lower"
        note(f"metric {metric}: treating {direction} as better (edit higher_is_better if that's wrong)")
    if cfg.mode == "notebook" and not (root / cfg.kernel_dir).exists():
        write_kernel(root / cfg.kernel_dir, user or "your-username", slug, cfg.kernel_output)
        good(f"code competition: scaffolded {cfg.kernel_dir}/ (kernel-metadata.json + submission.py)")
    if info and info.joined is False:
        warn(f"you haven't joined yet. Accept the rules before submitting: {info.url}/rules")
    return load(root)


def cmd_init(args) -> int:
    init_project(Path(args.dir).resolve(), args.competition, args.force)
    return 0


def cmd_new(args) -> int:
    dest = Path(args.dir or args.competition).resolve()
    if dest.exists() and any(dest.iterdir()) and not args.force:
        raise KshipError(f"{dest} already exists and isn't empty")
    dest.mkdir(parents=True, exist_ok=True)
    headline(f"new competition project: {dest}")
    if shutil.which("git") and not (dest / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=dest, check=False)
    cfg = init_project(dest, args.competition, force=args.force)
    info_title = ""
    first = (dest / CONFIG_NAME).read_text().splitlines()[0]
    if first.startswith("# "):
        info_title = first[2:]
    for p in write_project(dest, args.competition, info_title, cfg.sample_submission):
        note(f"created {rel(p, dest)}")
    wf = write_workflow(dest, args.source or default_source())
    note(f"created {rel(wf, dest)}")
    print()
    print("Next:")
    print(f"  cd {rel(dest, Path.cwd())}")
    print("  make setup && make data")
    print("  # edit src/pipeline.py, then:")
    print('  make ship MSG="first model"        # locally, or push and let CI ship on merge')
    print()
    _secret_help(dest)
    return 0


def _secret_help(root: Path) -> None:
    token_file = Path.home() / ".kaggle" / "access_token"
    print("For CI, give the repo your Kaggle token as a secret (run inside the repo, after pushing it to GitHub):")
    if token_file.is_file():
        print("  gh secret set KAGGLE_API_TOKEN < ~/.kaggle/access_token")
    else:
        print("  gh secret set KAGGLE_API_TOKEN      # paste a KGAT_... token from https://www.kaggle.com/settings/api")


def cmd_ci(args) -> int:
    cfg = load()
    path = cfg.root / ".github" / "workflows" / "kship.yml"
    existed = path.exists()
    if existed and not args.force:
        raise KshipError(f"{rel(path, Path.cwd())} already exists (use --force to overwrite)")
    write_workflow(cfg.root, args.source or default_source(), branch=args.branch, force=True)
    good(f"{'rewrote' if existed else 'wrote'} {rel(path, Path.cwd())}")
    note(
        "pull requests get a dry-run verdict as a comment; merges to "
        f"{args.branch} ship if CV beats your best on Kaggle"
    )
    if not (cfg.root / "Makefile").exists():
        warn("the workflow calls `make setup` and `src.pipeline`; adjust those two lines to fit this project")
    _secret_help(cfg.root)
    return 0


def cmd_data(args) -> int:
    cfg = load()
    k = make_kaggle()
    dest = cfg.root / "data"
    headline(f"downloading {cfg.competition} into {rel(dest, Path.cwd())}/")
    for p in k.download_all(cfg.competition, dest):
        note(f"{rel(p, cfg.root)}  ({p.stat().st_size / 1e6:.1f} MB)")
    return 0


# -- check / submit / run -----------------------------------------------------


def cmd_check(args) -> int:
    cfg = load()
    if cfg.mode == "notebook" and not args.file:
        raise KshipError("this is a code competition; pass a local CSV to check, e.g. kship check out.csv")
    res = check_submission(resolve_file(args.file, cfg), cfg.sample_path, cfg.value_range)
    report(res)
    return 0 if res.ok else 1


def ship(
    cfg: Config,
    path: Path,
    *,
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
    slug = cfg.competition
    higher = cfg.higher_is_better
    out = Outcome(decision="failed", reason="", competition=slug, cv=cv)
    say = {
        "shipped": lambda m: good(m),
        "would-ship": lambda m: good(f"dry run, nothing uploaded: would ship. {m}"),
        "skipped": lambda m: note(f"not shipped: {m}"),
        "blocked": lambda m: warn(f"not shipped: {m}"),
        "failed": lambda m: bad(m),
    }

    def finish(decision: str, reason: str) -> int:
        out.decision, out.reason = decision, reason
        say[decision](reason)
        emit(out)
        return out.code

    # 1. Validate what's about to be uploaded.
    if cfg.mode == "notebook":
        if not (path / "kernel-metadata.json").is_file():
            return finish("failed", f"{rel(path, cfg.root)}/kernel-metadata.json is missing")
        digest = dir_digest(path)
        good(f"kernel {rel(path, cfg.root)}/ ready to push")
    else:
        res = check_submission(path, cfg.sample_path, cfg.value_range)
        report(res)
        if not res.ok:
            return finish("failed", "fix the file first; uploading it would have cost a submission")
        digest = sha256(path)

    # 2. History = this machine's ledger + everything Kaggle has on record. Reading Kaggle
    #    means a fresh CI runner (or another laptop) gates against the same best score.
    k = None
    remote: list[dict] = []
    try:
        k = make_kaggle()
        remote = [ledger.from_kaggle(s) for s in k.submissions(slug, page_size=200)]
    except Exception as e:  # no credentials, 403 before the rules are accepted, network
        msg = str(e).splitlines()[0] if isinstance(e, KshipError) else explain(e)
        if not dry_run:
            return finish(
                "failed",
                f"can't read your submissions from Kaggle: {msg} "
                f"(rules: https://www.kaggle.com/competitions/{slug}/rules)",
            )
        warn(f"can't reach Kaggle ({msg}); judging against the local ledger only")
        k = None
    history = ledger.merge(ledger.load(cfg.ledger_path), remote)

    direction = higher if higher is not None else True
    prev_cv = ledger.best(history, "cv", higher) if higher is not None else None
    prev_pub = ledger.best(history, "public", direction)
    out.best_cv = prev_cv[0] if prev_cv else None
    out.best_public = prev_pub[0] if prev_pub else None

    # 3. Never pay twice for the same predictions.
    dup = ledger.find_duplicate(history, digest)
    if dup and not force:
        score = ledger.as_float(dup.get("public"))
        return finish(
            "skipped",
            f"identical to {ledger.label(dup)}"
            + (f" (public {score:.5f})" if score is not None else "")
            + ". Use --force to send it again.",
        )

    # 4. CV gate.
    verdict = ""
    if gate == "better":
        if cv is None:
            warn("no CV score to gate on (print KSHIP_CV=<score> or pass --cv); shipping anyway")
        elif higher is None:
            return finish(
                "failed",
                "set higher_is_better in kship.toml so the CV gate knows which way is up (or pass --gate always)",
            )
        elif prev_cv and not ledger.improves(cv, prev_cv[0], higher, cfg.min_improvement):
            return finish(
                "skipped",
                f"cv {cv:.5f} doesn't beat your best {prev_cv[0]:.5f} "
                f"({ledger.label(prev_cv[1])}). Use --gate always to ship anyway.",
            )
        elif prev_cv:
            verdict = f"cv {cv:.5f} beats your best {prev_cv[0]:.5f} ({ledger.label(prev_cv[1])})"
            good(verdict)
        else:
            verdict = f"cv {cv:.5f} is the first CV on record"

    git = ledger.git_info(cfg.root)
    message = build_message(note_text, cv, git, digest, path.name if cfg.mode == "file" else "kernel")
    out.message = message
    if git and git.get("dirty"):
        warn("uncommitted changes: this submission won't be reproducible from its commit alone")

    # 5. Respect the daily quota, keeping a reserve when automation is driving.
    lim = k.limits(slug) if k else None
    if lim is not None:
        out.left_today = lim.allowed_now
    if dry_run:
        return finish("would-ship", verdict or f"{rel(path, cfg.root)} passes every check")
    if lim is not None:
        floor = cfg.reserve if automated else 0
        if lim.allowed_now == 0 and lim.total == 0:
            return finish(
                "failed",
                f"Kaggle shows no submissions available for {slug}. If you haven't "
                f"joined, accept the rules: https://www.kaggle.com/competitions/{slug}/rules",
            )
        if lim.allowed_now == 0:
            return finish("blocked", f"daily limit reached ({lim.today} used today); it resets at 00:00 UTC")
        if lim.allowed_now <= floor:
            return finish(
                "blocked",
                f"{lim.allowed_now} submission(s) left today and reserve = {floor}, so "
                "automation won't spend them. Ship it with `kship submit`, or re-run "
                "after 00:00 UTC.",
            )

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
        return finish("failed", f"upload failed: {e if isinstance(e, KshipError) else explain(e)}")
    good(f"submitted to {slug}: {message}")

    rec = ledger.append(
        cfg.ledger_path,
        {
            "time": ledger.now(),
            "file": rel(path, cfg.root),
            "sha256": digest,
            "sub": digest[:8],
            "git": git,
            "cv": cv,
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
    if lim is not None:
        out.left_today = max(lim.allowed_now - 1, 0)

    if not wait:
        return finish("shipped", "submitted; not waiting for the score (`kship log --refresh` picks it up)")

    # 7. Wait for the leaderboard and record what came back.
    note(f"waiting up to {cfg.poll_minutes:g} min for the score...")
    try:
        sub = k.wait_for_score(slug, ref, message, cfg.poll_minutes * 60)
    except Exception as e:
        warn(f"lost track of the score ({e})")
        sub = None
    records = ledger.load(cfg.ledger_path)
    current = next(r for r in records if r["n"] == rec["n"])
    if sub is not None:
        current["ref"] = current.get("ref") or sub.ref
        current["status"] = ledger.KAGGLE_STATUS.get(sub.status, "pending")
        current["public"], current["private"] = sub.public, sub.private
        if sub.error:
            current["error"] = sub.error
    ledger.save(cfg.ledger_path, records)

    if current["status"] == "error":
        notify("kship: submission failed", current.get("error") or slug)
        return finish("failed", f"Kaggle couldn't score it: {current.get('error') or 'no details'}")
    if current["public"] is None:
        return finish("shipped", "submitted; still scoring, run `kship log --refresh` in a bit")

    public = out.public = current["public"]
    line = f"public score {public:.5f}"
    if prev_pub:
        delta = public - prev_pub[0]
        line += f" ({'+' if delta >= 0 else ''}{delta:.5f} vs your best {prev_pub[0]:.5f}"
        line += ", new best!)" if ledger.improves(public, prev_pub[0], direction) else ")"
    notify(f"kship: {slug}", line)
    return finish("shipped", line)


def cmd_submit(args) -> int:
    cfg = load()
    path = cfg.root / cfg.kernel_dir if cfg.mode == "notebook" else resolve_file(args.file, cfg)
    return ship(
        cfg,
        path,
        note_text=args.message or "",
        cv=args.cv,
        gate=args.gate,
        automated=False,
        force=args.force,
        dry_run=args.dry_run,
        wait=not args.no_wait,
    )


_MARKER = re.compile(rb"^\s*KSHIP_(CV|FILE|MSG)=(.*?)\s*$")


def run_streaming(cmd: list[str], env: dict) -> tuple[int, dict]:
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


def cmd_run(args) -> int:
    cfg = load()
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise KshipError("give the pipeline command after --, e.g.  kship run -- python -m src.pipeline")

    started = time.time() - 1
    env = {**os.environ, "KSHIP": "1", "PYTHONUNBUFFERED": "1", "KSHIP_SUBMISSION_DIR": str(cfg.submissions_path)}
    headline(f"kship run: {' '.join(cmd)}")
    rc, markers = run_streaming(cmd, env)

    def fail(reason: str) -> int:
        bad(reason)
        emit(Outcome(decision="failed", reason=reason, competition=cfg.competition))
        return EXIT_FAILED  # never the pipeline's own code: 3 means "quota" to a workflow

    if rc != 0:
        return fail(f"pipeline exited with status {rc}; nothing shipped")

    cv = args.cv
    if cv is None and "cv" in markers:
        try:
            cv = float(markers["cv"])
        except ValueError:
            warn(f"ignoring unparseable KSHIP_CV={markers['cv']}")

    if cfg.mode == "notebook":
        path = cfg.root / cfg.kernel_dir
    elif "file" in markers:
        path = Path(markers["file"]).expanduser()
        path = path if path.is_absolute() else (Path.cwd() / path)
    else:
        path = newest_csv(cfg.submissions_path, since=started)
        if path is None:
            return fail(
                f"the pipeline finished but wrote no new file to {cfg.submission_dir}/ "
                "(write it there, or print KSHIP_FILE=<path>)"
            )
    print()
    headline("shipping")
    return ship(
        cfg,
        path.resolve(),
        note_text=args.message or markers.get("msg", ""),
        cv=cv,
        gate=args.gate,
        automated=True,
        force=args.force,
        dry_run=args.dry_run,
        wait=not args.no_wait,
        command=" ".join(cmd),
    )


# -- log ----------------------------------------------------------------------


def cmd_log(args) -> int:
    cfg = load()
    records = ledger.load(cfg.ledger_path)
    if args.refresh:
        k = make_kaggle()
        live = {s.ref: s for s in k.submissions(cfg.competition, page_size=200)}
        changed = 0
        for r in records:
            s = live.pop(str(r.get("ref")), None)
            if s is None:
                continue
            new = {"status": ledger.KAGGLE_STATUS.get(s.status, "pending"), "public": s.public, "private": s.private}
            if any(r.get(key) != val for key, val in new.items()):
                r.update(new)
                changed += 1
        # Submissions made elsewhere (CI, another machine, the website) join the ledger too.
        imported = sorted((ledger.from_kaggle(s) for s in live.values()), key=lambda r: r.get("time") or "")
        n = max((r.get("n", 0) for r in records), default=0)
        for r in imported:
            n += 1
            records.append({"n": n, **r})
        records.sort(key=lambda r: r["n"])
        ledger.save(cfg.ledger_path, records)
        note(f"refreshed from Kaggle: {changed} updated, {len(imported)} imported")

    if not records:
        note("no submissions yet")
        return 0
    print(ledger.render_table(records))
    higher = cfg.higher_is_better if cfg.higher_is_better is not None else True
    for key in ("cv", "public", "private"):
        b = ledger.best(records, key, higher)
        if b:
            note(f"best {key}: {b[0]:.5f} ({ledger.label(b[1])}, {ledger.git_label(b[1].get('git'))})")
    verdict = ledger.cv_lb_verdict(records)
    if verdict:
        print()
        print(verdict)
    if args.md:
        out = cfg.root / "SUBMISSIONS.md"
        out.write_text(ledger.render_markdown(records, cfg.competition))
        good(f"wrote {rel(out, Path.cwd())}")
    return 0


# -- entry point --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kship",
        description="Validate, gate and ship Kaggle submissions straight from your pipeline.",
    )
    p.add_argument("-V", "--version", action="version", version=f"kship {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    s = sub.add_parser("doctor", help="check the CLI, credentials, and (optionally) a competition")
    s.add_argument("-c", "--competition", help="competition slug to check (defaults to kship.toml)")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("new", help="scaffold a new competition project with a ship-ready pipeline")
    s.add_argument("competition", help="competition slug, as in kaggle.com/competitions/<slug>")
    s.add_argument("dir", nargs="?", help="folder to create (default: the slug)")
    s.add_argument("--source", default=None, help="where CI installs kship from (default: this repo, see README)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("init", help="add kship.toml to an existing project")
    s.add_argument("competition", help="competition slug")
    s.add_argument("--dir", default=".", help="project folder (default: here)")
    s.add_argument("--force", action="store_true", help="overwrite an existing kship.toml")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("ci", help="add a GitHub Actions workflow: dry run on PRs, ship on merge")
    s.add_argument("--branch", default="main", help="branch whose pushes ship (default: main)")
    s.add_argument("--source", default=None, help="where CI installs kship from (default: this repo, see README)")
    s.add_argument("--force", action="store_true", help="overwrite an existing workflow")
    s.set_defaults(func=cmd_ci)

    s = sub.add_parser("data", help="download all competition files into data/")
    s.set_defaults(func=cmd_data)

    s = sub.add_parser("check", help="validate a submission against the sample, without uploading")
    s.add_argument("file", nargs="?", help="CSV to check (default: newest in submission_dir)")
    s.set_defaults(func=cmd_check)

    def ship_opts(s, gate_default: str) -> None:
        s.add_argument("-m", "--message", help="note for this submission")
        s.add_argument("--cv", type=float, help="local cross-validation score")
        s.add_argument(
            "--gate",
            choices=["better", "always"],
            default=gate_default,
            help=f"'better' ships only if CV beats your best (default: {gate_default})",
        )
        s.add_argument("--force", action="store_true", help="ship even if identical to an earlier submission")
        s.add_argument("--dry-run", action="store_true", help="run every check, upload nothing")
        s.add_argument("--no-wait", action="store_true", help="don't wait for the leaderboard score")

    s = sub.add_parser("submit", help="validate and upload a submission now")
    s.add_argument("file", nargs="?", help="CSV to submit (default: newest in submission_dir)")
    ship_opts(s, "always")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser(
        "run",
        help="run your pipeline, then ship its output if it improved",
        usage="kship run [options] -- <command ...>",
    )
    ship_opts(s, "better")
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="pipeline command, after --")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("log", help="submission history with scores and CV↔LB agreement")
    s.add_argument("--refresh", action="store_true", help="pull latest public/private scores from Kaggle")
    s.add_argument("--md", action="store_true", help="also write SUBMISSIONS.md")
    s.set_defaults(func=cmd_log)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KshipError as e:
        bad(str(e))
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as e:  # the Kaggle client raises plain requests errors
        if os.environ.get("KSHIP_DEBUG"):
            raise
        bad(explain(e))
        note("set KSHIP_DEBUG=1 to see the full traceback")
        return 1
