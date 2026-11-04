import inspect
import json
import sys
import tomllib

from conftest import SLUG, write_sub

from kship import cli, ledger


def entries(root):
    return ledger.load(root / "submissions.jsonl")


# -- init / new / doctor ------------------------------------------------------


def test_init_downloads_sample_and_infers_direction(project):
    cfg = tomllib.loads((project / "kship.toml").read_text())
    assert cfg["competition"] == SLUG
    assert cfg["sample_submission"] == "data/sample_submission.csv"
    assert cfg["higher_is_better"] is True and cfg["mode"] == "file"
    assert (project / "data/sample_submission.csv").exists()


def test_init_lower_is_better_metric(api, tmp_path, monkeypatch):
    api.metric = "Root Mean Squared Error (RMSE)"
    monkeypatch.chdir(tmp_path)
    cli.main(["init", SLUG])
    assert tomllib.loads((tmp_path / "kship.toml").read_text())["higher_is_better"] is False


def test_init_code_competition_scaffolds_kernel(api, tmp_path, monkeypatch):
    api.code_only = True
    monkeypatch.chdir(tmp_path)
    cli.main(["init", SLUG])
    meta = json.loads((tmp_path / "kernel/kernel-metadata.json").read_text())
    assert meta["id"] == f"haydn/{SLUG}-submission" and meta["competition_sources"] == [SLUG]
    assert meta["enable_internet"] is False
    assert tomllib.loads((tmp_path / "kship.toml").read_text())["mode"] == "notebook"


def test_new_scaffolds_a_project(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["new", SLUG]) == 0
    root = tmp_path / SLUG
    for f in ("kship.toml", "Makefile", "src/pipeline.py", ".gitignore", "README.md", "requirements.txt"):
        assert (root / f).exists(), f
    assert "\tkship run" in (root / "Makefile").read_text()
    assert "data/sample_submission.csv" in (root / "src/pipeline.py").read_text()
    compile((root / "src/pipeline.py").read_text(), "pipeline.py", "exec")


def test_doctor_without_credentials(api, capsys):
    assert cli.main(["doctor"]) == 1
    assert "kaggle auth login" in capsys.readouterr().out


def test_doctor_with_oauth_credentials(api, tmp_path, capsys):
    kdir = tmp_path / "home/.kaggle"
    kdir.mkdir()
    (kdir / "credentials.json").write_text("{}")
    (kdir / "credentials.json").chmod(0o644)
    assert cli.main(["doctor", "-c", SLUG]) == 0
    out = capsys.readouterr().out
    assert "signed in as haydn" in out and "chmod 600" in out and "5 submissions left today" in out


def test_doctor_flags_unjoined_competition(api, tmp_path, capsys):
    (tmp_path / "home/.kaggle").mkdir()
    (tmp_path / "home/.kaggle/access_token").write_text("KGAT_x")
    api.joined = False
    assert cli.main(["doctor", "-c", SLUG]) == 1
    assert "/rules" in capsys.readouterr().out


# -- submit -------------------------------------------------------------------


def test_submit_records_score_and_provenance(project, api):
    write_sub(project, "a.csv")
    assert cli.main(["submit", "-m", "baseline", "--cv", "0.88"]) == 0
    (e,) = entries(project)
    assert e["status"] == "scored" and e["public"] == 0.9 and e["cv"] == 0.88
    assert e["message"].startswith("baseline · cv=0.88000 · git=no-git · sub=")
    assert e["sub"] == e["sha256"][:8]
    assert api.uploads[0][0].endswith("submissions/a.csv")


def test_invalid_file_never_uploads(project, api):
    write_sub(project, "bad.csv", ids=(1, 2, 3, 99))
    assert cli.main(["submit"]) == 1
    assert api.uploads == [] and entries(project) == []


def test_identical_file_is_skipped(project, api):
    p = write_sub(project, "a.csv")
    cli.main(["submit", str(p)])
    write_sub(project, "copy.csv")
    assert cli.main(["submit"]) == 0
    assert len(api.uploads) == 1
    assert cli.main(["submit", "--force"]) == 0 and len(api.uploads) == 2


def test_daily_limit_and_not_joined(project, api, capsys):
    write_sub(project, "a.csv")
    api.allowed_now = 0
    assert cli.main(["submit"]) == 3 and "resets at 00:00 UTC" in capsys.readouterr().out
    api.total = 0
    assert cli.main(["submit"]) == 1 and "/rules" in capsys.readouterr().out
    assert api.uploads == []


def test_scoring_error_is_recorded(project, api):
    write_sub(project, "a.csv")
    api.fail_score = True
    assert cli.main(["submit"]) == 1
    (e,) = entries(project)
    assert e["status"] == "error" and e["error"] == "bad file"


def test_still_pending_after_timeout(project, api):
    write_sub(project, "a.csv")
    api.score_after_polls = 10**9
    toml = project / "kship.toml"
    toml.write_text(toml.read_text().replace("poll_minutes = 10", "poll_minutes = 0"))
    assert cli.main(["submit"]) == 0
    assert entries(project)[0]["status"] == "pending"


def test_dry_run_uploads_nothing(project, api):
    write_sub(project, "a.csv")
    assert cli.main(["submit", "--dry-run"]) == 0 and api.uploads == []


# -- run ----------------------------------------------------------------------


def pipeline(cv, name, fail=False):
    code = (
        "import pathlib,sys,os;"
        f"p=pathlib.Path(os.environ['KSHIP_SUBMISSION_DIR'])/'{name}';p.parent.mkdir(exist_ok=True);"
        f"p.write_text('id,target\\n1,{cv if cv is not None else 0.5}\\n2,0.2\\n3,0.3\\n4,0.4\\n');"
        "print('training...');"
        + (f"print('KSHIP_CV={cv}');" if cv is not None else "")
        + ("sys.exit(3)" if fail else "")
    )
    return ["run", "--", sys.executable, "-c", code]


def test_run_ships_only_when_cv_improves(project, api, capsys):
    assert cli.main(pipeline(0.80, "r1.csv")) == 0
    assert cli.main(pipeline(0.79, "r2.csv")) == 0
    assert "doesn't beat your best" in capsys.readouterr().out
    assert cli.main(pipeline(0.83, "r3.csv")) == 0
    assert [e["cv"] for e in entries(project)] == [0.80, 0.83]
    assert all(e["automated"] for e in entries(project))


def test_run_failure_ships_nothing(project, api):
    assert cli.main(pipeline(0.9, "x.csv", fail=True)) == 1
    assert api.uploads == []


def test_run_keeps_a_reserve_slot(project, api, capsys):
    api.allowed_now = 1
    assert cli.main(pipeline(0.9, "x.csv")) == 3
    assert "reserve = 1" in capsys.readouterr().out and api.uploads == []
    assert cli.main(["submit"]) == 0 and len(api.uploads) == 1  # manual submit may spend it


def test_run_without_new_file_errors(project, api, capsys):
    assert cli.main(["run", "--", sys.executable, "-c", "print('hi')"]) == 1
    assert "wrote no new file" in capsys.readouterr().out


def test_run_without_cv_still_ships(project, api, capsys):
    assert cli.main(pipeline(None, "n.csv")) == 0
    assert "no CV score to gate on" in capsys.readouterr().out and len(api.uploads) == 1


# -- notebook mode ------------------------------------------------------------


def test_code_competition_pushes_kernel_then_submits(api, tmp_path, monkeypatch):
    api.code_only = True
    monkeypatch.chdir(tmp_path)
    cli.main(["init", SLUG])
    assert cli.main(["submit", "-m", "nb"]) == 0
    assert api.kernel_pushes == 1 and api.kernel_polls == 2
    (sub,) = api.code_submits
    assert sub[0] == "submission.csv" and sub[3] == f"haydn/{SLUG}-submission" and sub[4] == 7
    assert entries(tmp_path)[0]["kernel_version"] == 7


# -- log ----------------------------------------------------------------------


def test_log_refresh_and_markdown(project, api, capsys):
    write_sub(project, "a.csv")
    cli.main(["submit", "--no-wait", "--cv", "0.8"])
    assert entries(project)[0]["status"] == "pending"
    assert cli.main(["log", "--refresh", "--md"]) == 0
    assert entries(project)[0]["public"] == 0.9
    assert "| 1 |" in (project / "SUBMISSIONS.md").read_text()
    assert "best public: 0.90000" in capsys.readouterr().out


# -- the real Kaggle API still has the shape kship relies on -----------------


def test_real_kaggle_api_surface():
    from kaggle.api.kaggle_api_extended import KaggleApi

    expect = {
        "competitions_list": {"group", "search", "category", "page_size"},
        "competition_get_submission_limits": set(),
        "competition_list_files": {"page_size"},
        "competition_download_file": {"path", "force", "quiet"},
        "competition_download_files": {"path", "force", "quiet"},
        "competition_submit": {"quiet"},
        "competition_submit_code": {"quiet"},
        "competition_submissions": {"page_size"},
        "kernels_push": set(),
        "kernels_status": set(),
    }
    for name, kwargs in expect.items():
        params = set(inspect.signature(getattr(KaggleApi, name)).parameters)
        assert kwargs <= params, (name, kwargs - params)
    assert KaggleApi.COMPETITION_SUBMIT_UPLOAD_FAILED_MESSAGE


# -- Kaggle as the source of truth (what makes CI work) ----------------------


def remote_sub(api, message, public="0.95", status=None):
    from types import SimpleNamespace as NS

    from conftest import Status

    api.subs.insert(
        0,
        NS(
            ref=str(5000 + len(api.subs)),
            status=status or Status.COMPLETE,
            public_score=public,
            private_score="",
            description=message,
            error_description="",
            date="2026-11-01",
        ),
    )


def test_gate_uses_scores_submitted_from_elsewhere(project, api, capsys):
    # A CI runner (or another laptop) shipped cv 0.90; this machine's ledger is empty.
    remote_sub(api, "from ci · cv=0.90000 · git=abc1234 · sub=deadbeef")
    assert cli.main(pipeline(0.85, "a.csv")) == 0
    assert "doesn't beat your best 0.90000 (Kaggle submission 5000)" in capsys.readouterr().out
    assert api.uploads == []
    assert cli.main(pipeline(0.91, "b.csv")) == 0 and len(api.uploads) == 1


def test_dedupe_uses_fingerprint_from_kaggle(project, api):
    from kship.check import sha256

    p = write_sub(project, "a.csv")
    remote_sub(api, f"x · git=abc1234 · sub={sha256(p)[:8]}")
    assert cli.main(["submit"]) == 0 and api.uploads == []


def test_note_cannot_spoof_tags(project, api):
    write_sub(project, "a.csv")
    cli.main(["submit", "-m", "trick cv=0.99999"])
    msg = api.uploads[0][1]
    assert "cv=0.99999" not in msg and "trick cv:0.99999" in msg


def test_log_refresh_imports_submissions_made_elsewhere(project, api, capsys):
    remote_sub(api, "website upload")
    remote_sub(api, "from ci · cv=0.88000 · git=abc1234 · sub=deadbeef", public="0.87")
    assert cli.main(["log", "--refresh"]) == 0
    recs = entries(project)
    assert [r["n"] for r in recs] == [1, 2] and {r.get("cv") for r in recs} == {None, 0.88}
    assert "2 imported" in capsys.readouterr().out


# -- GitHub Actions -----------------------------------------------------------


def actions_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    summary, output = tmp_path / "summary.md", tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    return summary, output


def test_ci_dry_run_reports_would_ship(project, api, monkeypatch, tmp_path, capsys):
    summary, output = actions_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KSHIP_SUMMARY_FILE", str(tmp_path / "pr.md"))
    remote_sub(api, "old · cv=0.80000 · git=abc1234 · sub=deadbeef", public="0.79")
    assert cli.main(["run", "--dry-run"] + pipeline(0.85, "a.csv")[1:]) == 0
    assert api.uploads == []
    text = summary.read_text()
    assert "**Would ship**" in text and "| CV | 0.85000 | 0.80000 |" in text
    assert (tmp_path / "pr.md").read_text() == text.rstrip("\n") + "\n"
    assert "decision=would-ship" in output.read_text() and "shipped=false" in output.read_text()
    assert "::notice title=kship::Would ship" in capsys.readouterr().out


def test_ci_dry_run_is_not_blocked_by_quota(project, api, monkeypatch, tmp_path):
    actions_env(monkeypatch, tmp_path)
    api.allowed_now = 0
    assert cli.main(["run", "--dry-run"] + pipeline(0.85, "a.csv")[1:]) == 0


def test_ci_ship_writes_outputs(project, api, monkeypatch, tmp_path):
    summary, output = actions_env(monkeypatch, tmp_path)
    assert cli.main(pipeline(0.85, "a.csv")) == 0
    out = output.read_text()
    assert "decision=shipped" in out and "shipped=true" in out and "public=0.9" in out
    assert entries(project)[0]["ci"] is True


def test_ci_quota_block_is_exit_3_with_warning(project, api, monkeypatch, tmp_path, capsys):
    actions_env(monkeypatch, tmp_path)
    api.allowed_now = 1
    assert cli.main(pipeline(0.85, "a.csv")) == 3
    assert "::warning title=kship::" in capsys.readouterr().out


def test_pipeline_exit_3_is_not_mistaken_for_quota(project, api):
    code = "import sys; sys.exit(3)"
    assert cli.main(["run", "--", sys.executable, "-c", code]) == 1


def test_ci_command_writes_workflow(project, api, capsys):
    assert cli.main(["ci"]) == 0
    wf = (project / ".github/workflows/kship.yml").read_text()
    assert "secrets.KAGGLE_API_TOKEN" in wf and "kship run --dry-run" in wf
    assert f"KSHIP_SOURCE: {cli.default_source()}" in wf
    assert "${{ github.event.head_commit.message }}" in wf and "\"${NOTE%%$'\\n'*}\"" in wf
    assert cli.main(["ci"]) == 1  # refuses to overwrite
    import yaml  # the workflow must be valid YAML

    doc = yaml.safe_load(wf)
    steps = [s.get("name") or s.get("uses") for s in doc["jobs"]["pipeline"]["steps"]]
    assert "Ship to Kaggle if CV improved" in steps


def test_data_unwraps_single_folder_zip(project, api):
    assert cli.main(["data"]) == 0
    assert (project / "data/train.csv").exists() and (project / "data/sample_submission.csv").exists()
    assert not (project / "data/for_students").exists() and not (project / f"data/{SLUG}.zip").exists()


def test_doctor_in_ci_points_at_the_repo_secret(api, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("KAGGLE_API_TOKEN", "")  # what an unset secret looks like
    assert cli.main(["doctor"]) == 1
    assert "gh secret set KAGGLE_API_TOKEN" in capsys.readouterr().out


def test_explain_turns_http_errors_into_advice():
    from types import SimpleNamespace as NS

    from kship.kapi import explain

    class HTTPError(Exception):
        def __init__(self, code):
            super().__init__("boom")
            self.response = NS(status_code=code)

    assert "accepted this competition's rules" in explain(HTTPError(403))
    assert "rejected your credentials" in explain(HTTPError(401))
    assert explain(ValueError("x" * 500)).endswith("...")


# -- where CI installs kship from ---------------------------------------------


class FakeDist:
    def __init__(self, direct=None, urls=()):
        self._direct = direct
        self.metadata = type("M", (), {"get_all": lambda _self, key: list(urls) or None})()

    def read_text(self, name):
        return json.dumps(self._direct) if self._direct is not None else None


def test_source_env_override(monkeypatch):
    monkeypatch.setenv("KSHIP_SOURCE", "git+https://example.com/fork@main")
    assert cli.default_source() == "git+https://example.com/fork@main"


def test_source_reuses_git_install(monkeypatch):
    monkeypatch.delenv("KSHIP_SOURCE", raising=False)
    direct = {
        "url": "https://github.com/someone/kship-fork.git",
        "vcs_info": {"vcs": "git", "requested_revision": "v0.2.0", "commit_id": "abc"},
    }
    monkeypatch.setattr(cli.metadata, "distribution", lambda name: FakeDist(direct))
    assert cli.default_source() == "git+https://github.com/someone/kship-fork.git@v0.2.0"


def test_source_follows_repository_url_for_local_installs(monkeypatch):
    monkeypatch.delenv("KSHIP_SOURCE", raising=False)
    editable = {"url": "file:///Users/haydn/kship", "dir_info": {"editable": True}}
    dist = FakeDist(editable, urls=["Repository, https://github.com/haydn-s/kaggle-ship.git"])
    monkeypatch.setattr(cli.metadata, "distribution", lambda name: dist)
    assert cli.default_source() == f"git+https://github.com/haydn-s/kaggle-ship@v{cli.__version__}"


def test_generated_workflow_passes_actionlint(tmp_path):
    import shutil
    import subprocess
    import sysconfig

    import pytest

    from kship.ci import render_workflow

    exe = shutil.which("actionlint") or shutil.which("actionlint", path=sysconfig.get_path("scripts"))
    if exe is None:
        pytest.skip("actionlint not installed")
    wf = tmp_path / ".github/workflows/kship.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(render_workflow("git+https://github.com/haydn-s/kship@v0.1.0"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    result = subprocess.run([exe], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
