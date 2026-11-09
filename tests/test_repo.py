"""This repo's own GitHub setup: main's ruleset has to require a check that CI actually reports."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_main_ruleset_requires_a_check_that_covers_every_job():
    ruleset = json.loads((ROOT / ".github" / "rulesets" / "main.json").read_text())
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())["jobs"]
    rules = {r["type"]: r.get("parameters", {}) for r in ruleset["rules"]}

    assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert {"pull_request", "non_fast_forward", "deletion"} <= set(rules)
    contexts = [c["context"] for c in rules["required_status_checks"]["required_status_checks"]]
    assert contexts, "the ruleset requires no checks"
    for context in contexts:
        # A renamed job would leave PRs waiting forever on a check that never reports.
        matches = [job_id for job_id, job in jobs.items() if job.get("name", job_id) == context]
        assert matches, f"the ruleset requires {context!r}, but test.yml has no such job"
        gate = jobs[matches[0]]
        # A required check that's skipped counts as passing, so the gate must always run,
        # and it must wait on every other job, including ones added later.
        assert gate.get("if") == "always()"
        assert set(gate["needs"]) == set(jobs) - {matches[0]}


def test_repo_workflows_pass_actionlint():
    import shutil
    import subprocess
    import sysconfig

    import pytest

    exe = shutil.which("actionlint") or shutil.which("actionlint", path=sysconfig.get_path("scripts"))
    if exe is None:
        pytest.skip("actionlint not installed")
    result = subprocess.run([exe], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
