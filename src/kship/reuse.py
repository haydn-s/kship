"""Carry a pipeline's cache from a pull request's CI run to the run after its merge.

With `on_merge = "reuse"` in kship.toml, the workflow's pull request run packs the folders in
`[ci] cache` into an artifact (`kship reuse save`), and the run on main restores them before it
runs the pipeline (`kship reuse restore`), so work the pull request's run already did isn't
repeated. Whether a restored file is still current is the pipeline's call: kship restores what
the pull request's run left and says whether main is exactly what that run tested, but it
can't know what each file depends on.

Two things kship does guarantee: restored files never replace a file that's in the commit, and
nothing lands outside the `cache` paths that main's own kship.toml names.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from kship import KshipError
from kship.config import Config

ARTIFACT = "kship-reuse"  # the artifact the workflow uploads on pull requests
MANIFEST = "kship-reuse.json"  # inside the archive: which commit and tree the files came from
RUNS_TO_TRY = 5  # a pull request's newest completed runs on its final commit (re-runs, retries)


class GhError(Exception):
    pass


def gh(*args: str) -> str:
    """Run the GitHub CLI (on every GitHub-hosted runner; authenticated by GH_TOKEN). Tests swap it."""
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise GhError("the GitHub CLI (gh) isn't installed on this runner") from None
    except subprocess.TimeoutExpired:
        raise GhError(f"gh {args[0]} timed out") from None
    if r.returncode != 0:
        lines = (r.stderr or r.stdout).strip().splitlines()
        raise GhError(lines[-1] if lines else f"gh exited with status {r.returncode}")
    return r.stdout


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return r.stdout.strip()


def _within(p: PurePosixPath, paths: tuple[str, ...]) -> bool:
    return any(p == PurePosixPath(c) or PurePosixPath(c) in p.parents for c in paths)


# -- save (pull request run) --------------------------------------------------


def save(cfg: Config, out: Path, only: list[str] | None = None) -> int:
    """Pack the cache paths (or `only`, which must sit inside them) into a .tar.gz at `out`.
    Returns how many files went in; 0 means nothing was written."""
    chosen = [PurePosixPath(p).as_posix() for p in only] if only else list(cfg.cache)
    for p in chosen:
        if not _within(PurePosixPath(p), cfg.cache):
            raise KshipError(f"{p} isn't inside [ci] cache ({', '.join(cfg.cache)}), so main wouldn't restore it")
    files = [f for p in chosen for f in _files(cfg.root / p)]
    if not files:
        return 0
    manifest = {
        "commit": _git(cfg.root, "rev-parse", "HEAD"),
        "tree": _git(cfg.root, "rev-parse", "HEAD^{tree}"),
        "paths": chosen,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        raw = json.dumps(manifest).encode()
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        for f in files:
            tar.add(f, arcname=f.relative_to(cfg.root).as_posix(), recursive=False)
    return len(files)


def _files(path: Path) -> list[Path]:
    if path.is_symlink():
        return []  # restore refuses links anyway
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink())
    return []


# -- restore (run on main) ----------------------------------------------------


@dataclass
class Restored:
    pr: int
    run_url: str
    files: list[str]  # restored, relative to the project root
    skipped: list[str]  # in the archive, but in the commit too, so left alone
    same_tree: bool | None  # main is exactly what the pull request's run tested (None: unknown)


def merged_pull_request(repo: str, sha: str) -> tuple[int, str] | None:
    """The pull request whose merge made commit `sha` (merge, squash or rebase): its number and
    the final commit of its branch. None for a direct push."""
    pulls = json.loads(gh("api", f"repos/{repo}/commits/{sha}/pulls") or "[]")
    for pr in pulls:
        if pr.get("merge_commit_sha") == sha and pr.get("merged_at"):
            return int(pr["number"]), str(pr["head"]["sha"])
    return None


def pull_request_runs(repo: str, workflow: str, head_sha: str) -> list[dict]:
    """This workflow's completed pull request runs on that commit, newest first."""
    out = gh(
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        workflow,
        "--commit",
        head_sha,
        "--event",
        "pull_request",
        "--status",
        "completed",
        "--limit",
        str(RUNS_TO_TRY),
        "--json",
        "databaseId,url",
    )
    return json.loads(out or "[]")


def workflow_file() -> str:
    """The running workflow's file name, from GITHUB_WORKFLOW_REF
    (owner/repo/.github/workflows/kship.yml@refs/heads/main)."""
    ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    return PurePosixPath(ref.partition("@")[0]).name or "kship.yml"


def restore(cfg: Config, name: str = ARTIFACT) -> Restored | str:
    """Restore the cache the merged pull request's run kept. Returns what was restored, or why
    nothing was (never raises for a miss: the pipeline then simply starts from scratch)."""
    repo, sha = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_SHA")
    if not repo or not sha:
        raise KshipError("kship reuse restore runs in GitHub Actions (it needs GITHUB_REPOSITORY and GITHUB_SHA)")
    try:
        found = merged_pull_request(repo, sha)
        if found is None:
            return f"no pull request was merged as {sha[:7]} (a direct push?)"
        pr, head = found
        runs = pull_request_runs(repo, workflow_file(), head)
    except (GhError, ValueError, KeyError) as e:
        return f"couldn't look up the pull request's run: {e}"
    if not runs:
        return f"pull request #{pr} has no finished run on its last commit ({head[:7]})"

    last_error = ""
    for run in runs:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                gh("run", "download", str(run["databaseId"]), "--repo", repo, "--name", name, "--dir", tmp)
            except GhError as e:
                last_error = str(e)
                continue
            archives = sorted(Path(tmp).rglob("*.tar.gz"))
            if not archives:
                last_error = f"artifact {name} holds no .tar.gz"
                continue
            try:
                manifest, files, skipped = unpack(archives[0], cfg)
            except (tarfile.TarError, OSError, ValueError) as e:
                return f"couldn't unpack pull request #{pr}'s {name}: {e}"
            tree = _git(cfg.root, "rev-parse", "HEAD^{tree}")
            same = (manifest.get("tree") == tree) if tree and manifest.get("tree") else None
            return Restored(pr, str(run.get("url", "")), files, skipped, same)
    why = f" ({last_error})" if last_error else ""
    return (
        f"pull request #{pr}'s run kept no {name} artifact{why}. It's kept only when on_merge = \"reuse\" "
        "was already on in that run, and expires after the workflow's retention-days"
    )


def unpack(archive: Path, cfg: Config) -> tuple[dict, list[str], list[str]]:
    """Extract the archive's files that fall inside main's own `[ci] cache`, except any that are
    in the commit. Returns the manifest, the files extracted, and the ones skipped as tracked."""
    root = cfg.root.resolve()
    tracked_out = _git(root, "ls-files", "-z", "--", *cfg.cache) or ""
    tracked = set(filter(None, tracked_out.split("\0")))
    manifest: dict = {}
    members, files, skipped = [], [], []
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            p = PurePosixPath(m.name)
            if m.name == MANIFEST and m.isfile():
                f = tar.extractfile(m)
                manifest = json.loads(f.read()) if f else {}
                continue
            if not m.isfile() or p.is_absolute() or ".." in p.parts or not _within(p, cfg.cache):
                continue  # links, devices, and anything outside cache: never written
            if not (root / p).resolve().is_relative_to(root):
                continue  # e.g. a symlinked folder in the checkout pointing elsewhere
            if p.as_posix() in tracked:
                skipped.append(p.as_posix())
                continue
            members.append(m)
            files.append(p.as_posix())
        if hasattr(tarfile, "data_filter"):
            tar.extractall(root, members=members, filter="data")
        else:  # Python before 3.11.4: the checks above are the whole guard
            tar.extractall(root, members=members)
    return manifest, files, skipped
