"""GitHub Actions integration: job summary, step outputs, annotations, and the workflow template."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# exit codes kship uses so a workflow can tell outcomes apart
EXIT_OK = 0  # shipped, would ship (dry run), or deliberately skipped
EXIT_FAILED = 1  # invalid file, pipeline failure, Kaggle rejected or couldn't score it
EXIT_BLOCKED = 3  # quota or reserve: a good submission is waiting for tomorrow's slots

HEADLINES = {
    "shipped": "Shipped",
    "would-ship": "Would ship",
    "skipped": "Not shipped",
    "blocked": "Blocked by the daily limit",
    "failed": "Failed",
}


@dataclass
class Outcome:
    decision: str  # shipped | would-ship | skipped | blocked | failed
    reason: str
    competition: str
    cv: float | None = None
    best_cv: float | None = None
    public: float | None = None
    best_public: float | None = None
    message: str = ""
    left_today: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def code(self) -> int:
        return {"failed": EXIT_FAILED, "blocked": EXIT_BLOCKED}.get(self.decision, EXIT_OK)


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _f(x: float | None) -> str:
    return "—" if x is None else f"{x:.5f}"


def summary_markdown(o: Outcome) -> str:
    lines = [f"### kship · {o.competition}", "", f"**{HEADLINES.get(o.decision, o.decision)}**: {o.reason}", ""]
    lines += ["| | this run | best before |", "|---|---|---|", f"| CV | {_f(o.cv)} | {_f(o.best_cv)} |"]
    if o.public is not None or o.best_public is not None:
        lines.append(f"| public LB | {_f(o.public)} | {_f(o.best_public)} |")
    if o.message:
        lines += ["", f"Submission message: `{o.message}`"]
    if o.left_today is not None:
        lines += ["", f"{o.left_today} submission(s) left today (resets 00:00 UTC)."]
    return "\n".join(lines) + "\n"


def emit(o: Outcome) -> None:
    """Hand the outcome to GitHub Actions (and to KSHIP_SUMMARY_FILE, used for PR comments)."""
    md = summary_markdown(o)
    if path := os.environ.get("KSHIP_SUMMARY_FILE"):
        Path(path).write_text(md)
    if not in_actions():
        return
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write(md + "\n")
    if path := os.environ.get("GITHUB_OUTPUT"):
        values = {
            "decision": o.decision,
            "shipped": str(o.decision == "shipped").lower(),
            "cv": "" if o.cv is None else f"{o.cv}",
            "best_cv": "" if o.best_cv is None else f"{o.best_cv}",
            "public": "" if o.public is None else f"{o.public}",
        }
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in values.items())
    level = {"failed": "error", "blocked": "warning"}.get(o.decision, "notice")
    text = f"{HEADLINES.get(o.decision, o.decision)}: {o.reason}".replace("%", "%25").replace("\n", "%0A")
    print(f"::{level} title=kship::{text}")


WORKFLOW = """\
# Runs the pipeline on every PR and on main.
#   pull request -> dry run: validates the submission and posts "would ship / wouldn't ship" on the PR
#   merge to main -> ships it to Kaggle if CV beats your best submission so far
# Needs one repo secret: KAGGLE_API_TOKEN (a KGAT_... token from kaggle.com/settings/api).
name: kship

on:
  pull_request:
  push:
    branches: [{branch}]
  workflow_dispatch:

concurrency:
  group: kship-${{{{ github.ref }}}}
  cancel-in-progress: ${{{{ github.event_name == 'pull_request' }}}}

permissions:
  contents: read
  pull-requests: write

env:
  KAGGLE_API_TOKEN: ${{{{ secrets.KAGGLE_API_TOKEN }}}}
  KSHIP_SOURCE: {source}

jobs:
  pipeline:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v7

      - uses: astral-sh/setup-uv@v10.3.3

      - name: Install kship and the project's dependencies
        run: |
          uv tool install "$KSHIP_SOURCE"
          make setup

      - name: Check Kaggle access
        run: kship doctor

      - name: Cache competition data
        id: data
        uses: actions/cache@v6
        with:
          path: data
          key: kaggle-data-${{{{ hashFiles('kship.toml') }}}}

      - name: Download competition data
        if: steps.data.outputs.cache-hit != 'true'
        run: kship data

      - name: Preview (dry run)
        if: github.event_name == 'pull_request'
        env:
          KSHIP_SUMMARY_FILE: ${{{{ runner.temp }}}}/kship.md
        run: kship run --dry-run -- {python} -m src.pipeline

      - name: Comment the preview on the PR
        if: always() && github.event_name == 'pull_request'
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          PR: ${{{{ github.event.pull_request.number }}}}
        run: |
          [ -f "$RUNNER_TEMP/kship.md" ] || exit 0
          gh pr comment "$PR" --body-file "$RUNNER_TEMP/kship.md" --edit-last --create-if-none

      - name: Ship to Kaggle if CV improved
        if: github.event_name != 'pull_request'
        env:
          # kship notes the subject line, or the PR title for "Merge pull request #N" commits
          NOTE: ${{{{ github.event.head_commit.message }}}}
        run: |
          set +e
          kship run -m "$NOTE" -- {python} -m src.pipeline
          rc=$?
          # 3 = out of daily submissions (already flagged in the summary); re-run this workflow tomorrow
          [ "$rc" -eq 3 ] && exit 0
          exit $rc

      - name: Keep the submission and ledger
        if: always()
        uses: actions/upload-artifact@v7
        with:
          name: submission-${{{{ github.sha }}}}
          path: |
            submissions/
            submissions.jsonl
          if-no-files-found: ignore
"""


def render_workflow(source: str, python: str = ".venv/bin/python", branch: str = "main") -> str:
    return WORKFLOW.format(source=source, python=python, branch=branch)


def write_workflow(root: Path, source: str, branch: str = "main", force: bool = False) -> Path:
    path = root / ".github" / "workflows" / "kship.yml"
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_workflow(source, branch=branch))
    return path
