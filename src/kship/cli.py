"""kship command line: doctor, new, init, data, check, submit, run, log."""

from __future__ import annotations

import argparse
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
from kship.check import check_submission
from kship.ci import EXIT_FAILED, Outcome, emit, in_actions, write_workflow
from kship.config import (
    CONFIG_NAME,
    MARKDOWN_NAME,
    Config,
    find_config,
    guess_higher_is_better,
    load,
    render,
)
from kship.kapi import AUTH_HELP, CompetitionInfo, Kaggle, credential_sources, explain
from kship.runner import newest_csv, run_streaming
from kship.scaffold import write_kernel, write_project
from kship.ship import place_sample, ship
from kship.ui import bad, good, headline, note, rel, report, say, warn

FALLBACK_REPO = "https://github.com/haydn-s/kship"


def default_source() -> str:
    """Where generated CI workflows install kship from.

    KSHIP_SOURCE wins. Otherwise, if this copy came from a package index (PyPI), the same
    version from there. If it was installed straight from git, exactly that (PEP 610
    direct_url.json). Otherwise (a local or editable install) the Repository URL in
    pyproject.toml, pinned to this version's tag.
    """
    if env := os.environ.get("KSHIP_SOURCE"):
        return env
    try:
        dist = metadata.distribution("kship")
    except metadata.PackageNotFoundError:
        return f"git+{FALLBACK_REPO}@v{__version__}"
    raw = dist.read_text("direct_url.json")
    if raw is None:  # only installs from an index have no direct_url.json
        return f"kship=={__version__}"
    try:
        direct = json.loads(raw or "{}")
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


def remote_has_tag(url: str, tag: str) -> bool | None:
    """Whether a git remote has a tag; None when it can't be asked (offline, private, no git)."""
    try:
        r = subprocess.run(
            ["git", "ls-remote", "--tags", "--exit-code", url, f"refs/tags/{tag}"],
            capture_output=True,
            timeout=15,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 2: False}.get(r.returncode)  # 2: reachable, but no such ref


def check_source(source: str) -> None:
    """Warn when the workflow would install kship from a tag that isn't pushed yet, which
    happens after bumping the version but before tagging it: every CI run would fail to install."""
    m = re.fullmatch(r"git\+(\S+?)@(v\d[\w.+-]*)", source)
    if m and remote_has_tag(*m.groups()) is False:
        url, tag = m.groups()
        warn(f"{url} has no tag {tag} yet, so CI can't install kship until you push it (or pass --source)")


# Tests swap these for fakes.
make_kaggle = Kaggle

# -- helpers ------------------------------------------------------------------


def resolve_file(arg: str | None, cfg: Config) -> Path:
    if arg:
        return Path(arg).resolve()
    found = newest_csv(cfg.submissions_path)
    if found is None:
        raise KshipError(f"no file given and nothing in {cfg.submission_dir}/ to pick up")
    return found


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


def init_project(root: Path, slug: str, force: bool = False) -> tuple[Config, CompetitionInfo | None]:
    """Write kship.toml (and fetch the sample). Returns the config, and the competition's
    details when Kaggle's search could find it."""
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
    return load(root), info


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
    cfg, info = init_project(dest, args.competition, force=args.force)
    for p in write_project(dest, args.competition, info.title if info else "", cfg.sample_submission):
        note(f"created {rel(p, dest)}")
    source = args.source or default_source()
    wf = write_workflow(dest, source)
    note(f"created {rel(wf, dest)}")
    check_source(source)
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
    source = args.source or default_source()
    write_workflow(cfg.root, source, branch=args.branch, force=True)
    good(f"{'rewrote' if existed else 'wrote'} {rel(path, Path.cwd())}")
    check_source(source)
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
    sample, placed = place_sample(cfg)
    say(placed)
    if sample is not None and not sample.is_file():
        warn(f"sample_submission in {CONFIG_NAME} ({cfg.sample_submission}) isn't among the downloaded files")
    return 0


# -- check / submit / run -----------------------------------------------------


def cmd_check(args) -> int:
    cfg = load()
    if cfg.mode == "notebook" and not args.file:
        raise KshipError("this is a code competition; pass a local CSV to check, e.g. kship check out.csv")
    sample, placed = place_sample(cfg)
    say(placed)
    res = check_submission(resolve_file(args.file, cfg), sample, cfg.value_range)
    report(res)
    return 0 if res.ok else 1


def cmd_submit(args) -> int:
    cfg = load()
    if cfg.mode == "notebook":
        if args.file:
            raise KshipError(
                f"this is a code competition: `kship submit` pushes {cfg.kernel_dir}/ and submits its output, "
                f"so it doesn't take a file (check one with `kship check {args.file}`)"
            )
        path = cfg.root / cfg.kernel_dir
    else:
        path = resolve_file(args.file, cfg)
    return ship(
        cfg,
        path,
        connect=make_kaggle,
        note_text=args.message or "",
        cv=args.cv,
        gate=args.gate,
        automated=False,
        force=args.force,
        dry_run=args.dry_run,
        wait=not args.no_wait,
    )


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
        newest = newest_csv(cfg.submissions_path, since=started)
        if newest is None:
            return fail(
                f"the pipeline finished but wrote no new file to {cfg.submission_dir}/ "
                "(write it there, or print KSHIP_FILE=<path>)"
            )
        path = newest
    print()
    headline("shipping")
    return ship(
        cfg,
        path.resolve(),
        connect=make_kaggle,
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
        live = {s.ref: s for s in k.all_submissions(cfg.competition)}
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
        for r in imported:
            records.append({"n": ledger.next_n(records), **r})
        records.sort(key=lambda r: r.get("n") or 0)
        ledger.save(cfg.ledger_path, records)
        note(f"refreshed from Kaggle: {changed} updated, {len(imported)} imported")

    if not records:
        note("no submissions yet")
        return 0
    print(ledger.render_table(records))
    higher = cfg.higher_is_better if cfg.higher_is_better is not None else True
    comparable = ledger.same_scheme(records, cfg.cv_scheme)
    for key in ("cv", "public", "private"):
        b = ledger.best(comparable if key == "cv" else records, key, higher)
        if b:
            label = f"best {key}" + (f" (cv_scheme {cfg.cv_scheme!r})" if key == "cv" and cfg.cv_scheme else "")
            note(f"{label}: {b[0]:.5f} ({ledger.label(b[1])}, {ledger.git_label(b[1].get('git'))})")
    verdict = ledger.cv_lb_verdict(comparable)
    if verdict:
        print()
        print(verdict)
    if args.md:
        out = cfg.root / MARKDOWN_NAME
        out.write_text(ledger.render_markdown(records, cfg.competition, cfg.cv_scheme))
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
