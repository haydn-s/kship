"""on_merge = "reuse": the pull request's run packs [ci] cache, the run on main restores it."""

import io
import json
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest
import yaml

from kship import cli, reuse
from kship.ci import render_workflow

REPO = "haydn/demo-comp"


def git(root, *args):
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    return subprocess.run(["git", *ident, *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def set_on_merge(root: Path, value: str, cache: str | None = None) -> None:
    cfg = root / "kship.toml"
    text = cfg.read_text().replace('on_merge = "retrain"', f'on_merge = "{value}"')
    if cache is not None:
        text = text.replace('cache = ["cache"]', f"cache = {cache}")
    cfg.write_text(text)


def commit_all(root: Path, msg: str = "work") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg)
    return git(root, "rev-parse", "HEAD")


class FakeGh:
    """Answers the three gh calls restore makes, like GitHub would after merging PR #7."""

    def __init__(self, merge_sha: str, archive: Path | None):
        merged = {"merge_commit_sha": merge_sha, "merged_at": "2026-11-22T10:00:00Z"}
        self.pulls = [{"number": 7, **merged, "head": {"sha": "f00d" * 10}}]
        self.runs = [{"databaseId": 11, "url": f"https://github.com/{REPO}/actions/runs/11"}]
        self.archive = archive
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "api":
            return json.dumps(self.pulls)
        if args[:2] == ("run", "list"):
            return json.dumps(self.runs)
        if args[:2] == ("run", "download"):
            if self.archive is None:
                raise reuse.GhError("no valid artifacts found to download")
            shutil.copy(self.archive, Path(args[args.index("--dir") + 1]) / "kship-reuse.tar.gz")
            return ""
        raise AssertionError(f"unexpected gh call: {args}")


@pytest.fixture
def actions(monkeypatch, tmp_path):
    """GitHub Actions' environment variables for a push to main; returns (outputs, summary)."""
    output, summary = tmp_path / "output.txt", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", f"{REPO}/.github/workflows/kship.yml@refs/heads/main")
    return output, summary


@pytest.fixture
def repo(project):
    """A competition repo with on_merge = "reuse", committed, with cache/ ignored like build output."""
    set_on_merge(project, "reuse")
    (project / ".gitignore").write_text("data/\nsubmissions/*.csv\ncache/\n")
    git(project, "init", "-q")
    commit_all(project, "start")
    return project


def pack_pr_cache(root: Path, tmp_path: Path, files: dict[str, str]) -> Path:
    """What the pull request's run leaves behind: its cache files, packed by `kship reuse save`."""
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    archive = tmp_path / "pr" / "kship-reuse.tar.gz"
    assert cli.main(["reuse", "save", str(archive)]) == 0
    for name in files:
        (root / name).unlink()  # main's run starts on a fresh runner
    return archive


# -- config -------------------------------------------------------------------


def test_new_projects_retrain_on_merge_by_default(project):
    cfg = tomllib.loads((project / "kship.toml").read_text())
    assert cfg["ci"] == {"on_merge": "retrain", "cache": ["cache"]}
    loaded = cli.load()
    assert loaded.on_merge == "retrain" and loaded.cache == ("cache",)


def test_kship_toml_without_a_ci_section_retrains(project):
    text = (project / "kship.toml").read_text()
    (project / "kship.toml").write_text(text.split("[ci]")[0])
    assert cli.load().on_merge == "retrain"


@pytest.mark.parametrize(
    ("on_merge", "cache", "complaint"),
    [
        ("skip", '["cache"]', 'on_merge must be "retrain" or "reuse"'),
        ("reuse", '["../elsewhere"]', "inside the project"),
        ("reuse", '["/tmp/cache"]', "inside the project"),
        ("reuse", '["."]', "can't be the whole project"),
        ("reuse", '[".git/objects"]', "inside .git"),
        ("reuse", "[]", "needs at least one folder"),
    ],
)
def test_bad_ci_settings_are_errors(project, capsys, on_merge, cache, complaint):
    set_on_merge(project, on_merge, cache)
    assert cli.main(["reuse", "save", "out.tar.gz"]) == 1
    assert complaint in capsys.readouterr().out


# -- save (pull request run) --------------------------------------------------


def test_save_does_nothing_when_retraining(project, actions, capsys):
    output, _ = actions
    (project / "cache").mkdir()
    (project / "cache" / "lgbm.npz").write_text("preds")
    assert cli.main(["reuse", "save", "out.tar.gz"]) == 0
    assert not (project / "out.tar.gz").exists()
    assert "saved=false" in output.read_text() and "nothing to keep" in capsys.readouterr().out


def test_save_packs_the_cache_with_the_commit_it_came_from(repo, actions, tmp_path):
    output, _ = actions
    (repo / "cache" / "deep").mkdir(parents=True)
    (repo / "cache" / "lgbm.npz").write_text("lgbm preds")
    (repo / "cache" / "deep" / "xgb.npz").write_text("xgb preds")
    assert cli.main(["reuse", "save", str(tmp_path / "a.tar.gz")]) == 0
    assert "saved=true" in output.read_text()
    with tarfile.open(tmp_path / "a.tar.gz") as tar:
        names = set(tar.getnames())
        manifest = json.load(tar.extractfile(reuse.MANIFEST))
    assert names == {reuse.MANIFEST, "cache/lgbm.npz", "cache/deep/xgb.npz"}
    assert manifest["tree"] == git(repo, "rev-parse", "HEAD^{tree}") and manifest["paths"] == ["cache"]


def test_save_with_an_empty_cache_keeps_nothing(repo, actions, capsys):
    output, _ = actions
    assert cli.main(["reuse", "save", "out.tar.gz"]) == 0
    assert "saved=false" in output.read_text() and not (repo / "out.tar.gz").exists()


def test_save_can_pack_one_file_for_a_job_per_model(repo, actions, tmp_path):
    (repo / "cache").mkdir()
    (repo / "cache" / "lgbm.npz").write_text("lgbm")
    (repo / "cache" / "xgb.npz").write_text("xgb")
    assert cli.main(["reuse", "save", str(tmp_path / "lgbm.tar.gz"), "cache/lgbm.npz"]) == 0
    with tarfile.open(tmp_path / "lgbm.tar.gz") as tar:
        assert set(tar.getnames()) == {reuse.MANIFEST, "cache/lgbm.npz"}


def test_save_refuses_paths_main_would_not_restore(repo, capsys):
    assert cli.main(["reuse", "save", "out.tar.gz", "src"]) == 1
    assert "isn't inside [ci] cache" in capsys.readouterr().out


# -- restore (run on main) ----------------------------------------------------


def test_restore_brings_back_the_pull_requests_cache(repo, actions, tmp_path, monkeypatch, capsys):
    output, summary = actions
    archive = pack_pr_cache(repo, tmp_path, {"cache/lgbm.npz": "lgbm preds", "cache/cat.npz": "cat preds"})
    sha = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("GITHUB_SHA", sha)
    fake = FakeGh(sha, archive)
    monkeypatch.setattr(reuse, "gh", fake)

    assert cli.main(["reuse", "restore"]) == 0
    assert (repo / "cache" / "lgbm.npz").read_text() == "lgbm preds"
    assert (repo / "cache" / "cat.npz").read_text() == "cat preds"
    assert not (repo / reuse.MANIFEST).exists()
    out = capsys.readouterr().out
    assert "restored 2 file(s) from pull request #7's run" in out and "exactly what that run tested" in out
    assert "restored=true" in output.read_text()
    assert "Started from pull request #7's cache" in summary.read_text()
    # it asked for this workflow's runs on the pull request's last commit, not on main's
    run_list = next(c for c in fake.calls if c[:2] == ("run", "list"))
    assert run_list[run_list.index("--commit") + 1] == "f00d" * 10
    assert run_list[run_list.index("--workflow") + 1] == "kship.yml"


def test_restore_says_when_main_differs_from_what_the_pr_tested(repo, actions, tmp_path, monkeypatch, capsys):
    archive = pack_pr_cache(repo, tmp_path, {"cache/lgbm.npz": "lgbm preds"})
    (repo / "src.py").write_text("another pull request merged first\n")
    sha = commit_all(repo, "other merge")
    monkeypatch.setenv("GITHUB_SHA", sha)
    monkeypatch.setattr(reuse, "gh", FakeGh(sha, archive))
    assert cli.main(["reuse", "restore"]) == 0
    assert "main also has changes that run didn't test" in capsys.readouterr().out
    assert (repo / "cache" / "lgbm.npz").exists()  # whether it's still current is the pipeline's call


def test_restore_never_replaces_a_committed_file(repo, actions, tmp_path, monkeypatch, capsys):
    (repo / "cache").mkdir()
    (repo / "cache" / "README.md").write_text("committed\n")
    git(repo, "add", "-f", "cache/README.md")
    sha = commit_all(repo, "document the cache")
    archive = tmp_path / "pr.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in {"cache/README.md": b"from the PR", "cache/lgbm.npz": b"preds"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    monkeypatch.setenv("GITHUB_SHA", sha)
    monkeypatch.setattr(reuse, "gh", FakeGh(sha, archive))
    assert cli.main(["reuse", "restore"]) == 0
    assert (repo / "cache" / "README.md").read_text() == "committed\n"
    assert (repo / "cache" / "lgbm.npz").read_text() == "preds"
    assert "left cache/README.md alone" in capsys.readouterr().out


def test_restore_writes_nothing_outside_main_s_cache_paths(repo, actions, tmp_path, monkeypatch):
    sha = git(repo, "rev-parse", "HEAD")
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("../escaped.txt", "/tmp/kship-absolute.txt", "src/pipeline.py", "data/train.csv", "cache/ok.npz"):
            info = tarfile.TarInfo(name)
            info.size = 2
            tar.addfile(info, io.BytesIO(b"x\n"))
        link = tarfile.TarInfo("cache/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    monkeypatch.setenv("GITHUB_SHA", sha)
    monkeypatch.setattr(reuse, "gh", FakeGh(sha, archive))
    assert cli.main(["reuse", "restore"]) == 0
    assert (repo / "cache" / "ok.npz").read_text() == "x\n"
    assert not (repo.parent / "escaped.txt").exists() and not Path("/tmp/kship-absolute.txt").exists()
    assert not (repo / "src" / "pipeline.py").exists() and not (repo / "data" / "train.csv").exists()
    assert not (repo / "cache" / "link").is_symlink()


def test_restore_after_a_direct_push_starts_from_scratch(repo, actions, monkeypatch, capsys):
    output, _ = actions
    sha = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("GITHUB_SHA", sha)
    fake = FakeGh(sha, None)
    fake.pulls = [{"number": 3, "merge_commit_sha": "0" * 40, "merged_at": None, "head": {"sha": sha}}]  # still open
    monkeypatch.setattr(reuse, "gh", fake)
    assert cli.main(["reuse", "restore"]) == 0
    out = capsys.readouterr().out
    assert "no pull request was merged as" in out and "starts from scratch" in out
    assert "restored=false" in output.read_text()


def test_restore_when_the_pr_run_kept_nothing(repo, actions, monkeypatch, capsys):
    sha = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("GITHUB_SHA", sha)
    fake = FakeGh(sha, None)
    fake.runs.append({"databaseId": 10, "url": "older"})
    monkeypatch.setattr(reuse, "gh", fake)
    assert cli.main(["reuse", "restore"]) == 0
    assert "kept no kship-reuse artifact" in capsys.readouterr().out
    assert sum(c[:2] == ("run", "download") for c in fake.calls) == 2  # tried each run


def test_restore_survives_github_api_errors(repo, actions, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_SHA", git(repo, "rev-parse", "HEAD"))

    def broken(*args):
        raise reuse.GhError("HTTP 403: Resource not accessible by integration")

    monkeypatch.setattr(reuse, "gh", broken)
    assert cli.main(["reuse", "restore"]) == 0
    assert "HTTP 403" in capsys.readouterr().out


def test_restore_does_nothing_when_retraining(project, actions, monkeypatch, capsys):
    output, _ = actions
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(reuse, "gh", lambda *a: pytest.fail("retrain mustn't call GitHub"))
    assert cli.main(["reuse", "restore"]) == 0
    assert "restored=false" in output.read_text() and "starts from scratch" in capsys.readouterr().out


def test_restore_outside_actions_says_where_it_runs(repo, monkeypatch, capsys):
    for var in ("GITHUB_REPOSITORY", "GITHUB_SHA"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["reuse", "restore"]) == 1
    assert "runs in GitHub Actions" in capsys.readouterr().out


# -- the workflow -------------------------------------------------------------


def test_workflow_saves_on_pull_requests_and_restores_before_shipping():
    doc = yaml.safe_load(render_workflow("git+https://github.com/haydn-s/kship@v0.2.0"))
    job = doc["jobs"]["pipeline"]
    steps = {s.get("name") or s["uses"]: s for s in job["steps"]}
    names = list(steps)

    pack, keep = steps["Pack the pipeline's cache for the merge"], steps["Keep the pipeline's cache for the merge"]
    assert pack["if"] == "github.event_name == 'pull_request'" and "kship reuse save" in pack["run"]
    assert keep["if"] == "steps.pack.outputs.saved == 'true'" and keep["with"]["name"] == reuse.ARTIFACT
    assert keep["continue-on-error"] is True  # a failed upload costs a retrain, not a red PR

    restore = steps["Start from the pull request's cache"]
    assert restore["if"] == "github.event_name != 'pull_request'" and restore["run"] == "kship reuse restore"
    assert names.index("Start from the pull request's cache") < names.index("Ship to Kaggle if CV improved")
    assert names.index("Preview (dry run)") < names.index("Pack the pipeline's cache for the merge")

    # The extra access is read-only, and only the restore step is handed the token.
    assert job["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    assert job["steps"][0]["with"]["persist-credentials"] is False
    given = {n for n, s in steps.items() if "github.token" in json.dumps(s.get("env") or {})}
    assert given == {"Start from the pull request's cache"}
