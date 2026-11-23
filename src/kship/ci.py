"""GitHub Actions integration: job summary, step outputs, annotations, and the workflow template."""

from __future__ import annotations

import os
import uuid
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
    file: str = ""  # what was judged, relative to the project root (a CSV, or kernel/)
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
            "file": o.file,
        }
        # The summary is multi-line, so it uses GitHub's heredoc form with an unguessable delimiter.
        delimiter = f"KSHIP_{uuid.uuid4().hex}"
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in values.items())
            f.write(f"summary<<{delimiter}\n{md}{delimiter}\n")
    level = {"failed": "error", "blocked": "warning"}.get(o.decision, "notice")
    text = f"{HEADLINES.get(o.decision, o.decision)}: {o.reason}".replace("%", "%25").replace("\n", "%0A")
    print(f"::{level} title=kship::{text}")


def set_outputs(**values: str) -> None:
    """Step outputs for later steps (`steps.<id>.outputs.<name>`); a no-op outside Actions."""
    if in_actions() and (path := os.environ.get("GITHUB_OUTPUT")):
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in values.items())


def add_summary(md: str) -> None:
    if in_actions() and (path := os.environ.get("GITHUB_STEP_SUMMARY")):
        with open(path, "a") as f:
            f.write(md.rstrip("\n") + "\n\n")


# Third-party actions are pinned to a commit, so a moved tag can't change what runs next to
# your Kaggle token. GitHub's own actions (actions/*) stay on version tags.
SETUP_UV = "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2  # v10.3.3"

WORKFLOW = """\
# Runs the pipeline on every PR and on main.
#   pull request -> dry run: validates the submission and posts "would ship / wouldn't ship" on the PR
#   merge to main -> ships it to Kaggle if CV beats your best submission so far
# Needs one repo secret: KAGGLE_API_TOKEN (a KGAT_... token from kaggle.com/settings/api).
# Only the steps that run kship get it: installing your dependencies doesn't, and neither does
# the job that writes the PR comment.
#
# By default the run on main trains everything again. With on_merge = "reuse" under [ci] in
# kship.toml, it first restores the folders in [ci] cache from the pull request's own run, so
# your pipeline can skip the work that run already did (see kship's README for what your
# pipeline has to check). Switching it on or off is just that line; this file stays the same.
name: kship

on:
  pull_request:
  push:
    branches: [{branch}]
  workflow_dispatch:

# One run at a time per branch, so two merges can't both ship against the same "best".
# GitHub keeps only the newest waiting run: with three quick merges, the middle one is
# cancelled and its commit never runs (the newest one does, and it includes that commit).
concurrency:
  group: kship-${{{{ github.ref }}}}
  cancel-in-progress: ${{{{ github.event_name == 'pull_request' }}}}

permissions:
  contents: read

env:
  KSHIP_SOURCE: {source}

jobs:
  pipeline:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    # Read-only extras for on_merge = "reuse": finding the pull request a merge came from, and
    # downloading what its run kept. Only the step that does that is handed the token.
    permissions:
      contents: read
      actions: read
      pull-requests: read
    outputs:
      summary: ${{{{ steps.preview.outputs.summary }}}}
    steps:
      - uses: actions/checkout@v7
        with:
          # nothing here pushes, so the job's token isn't left in .git/config for your pipeline
          persist-credentials: false

      - uses: {setup_uv}

      - name: Install kship and the project's dependencies
        run: |
          uv tool install "$KSHIP_SOURCE"
          make setup

      - name: Check Kaggle access
        env:
          KAGGLE_API_TOKEN: ${{{{ secrets.KAGGLE_API_TOKEN }}}}
        run: kship doctor

      - name: Cache competition data
        id: data
        uses: actions/cache@v6
        with:
          path: data
          key: kaggle-data-${{{{ hashFiles('kship.toml') }}}}

      - name: Download competition data
        if: steps.data.outputs.cache-hit != 'true'
        env:
          KAGGLE_API_TOKEN: ${{{{ secrets.KAGGLE_API_TOKEN }}}}
        run: kship data

      - name: Preview (dry run)
        id: preview
        if: github.event_name == 'pull_request'
        env:
          KAGGLE_API_TOKEN: ${{{{ secrets.KAGGLE_API_TOKEN }}}}
        run: kship run --dry-run -- {python} -m src.pipeline

      # on_merge = "reuse": keep [ci] cache for the run after the merge (a no-op otherwise)
      - name: Pack the pipeline's cache for the merge
        id: pack
        if: github.event_name == 'pull_request'
        run: kship reuse save "$RUNNER_TEMP/kship-reuse.tar.gz"

      - name: Keep the pipeline's cache for the merge
        if: steps.pack.outputs.saved == 'true'
        # e.g. out of artifact storage: the merge then trains from scratch, which is all it costs
        continue-on-error: true
        uses: actions/upload-artifact@v7
        with:
          name: kship-reuse
          path: ${{{{ runner.temp }}}}/kship-reuse.tar.gz
          # a pull request merged longer than this after its last run trains from scratch
          retention-days: 7
          compression-level: 0  # already gzipped
          overwrite: true       # re-running the job replaces it

      # on_merge = "reuse": restore what the merged pull request's run kept (a no-op otherwise)
      - name: Start from the pull request's cache
        if: github.event_name != 'pull_request'
        env:
          GH_TOKEN: ${{{{ github.token }}}}
        run: kship reuse restore

      - name: Ship to Kaggle if CV improved
        id: ship
        if: github.event_name != 'pull_request'
        env:
          KAGGLE_API_TOKEN: ${{{{ secrets.KAGGLE_API_TOKEN }}}}
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
          # just the file kship judged, not everything in submissions/
          path: |
            ${{{{ steps.preview.outputs.file || steps.ship.outputs.file || 'submissions.jsonl' }}}}
            submissions.jsonl
          if-no-files-found: ignore
          retention-days: 30

  # Separate from the job that runs your code, so only this job can write to the PR.
  comment:
    needs: pipeline
    if: always() && github.event_name == 'pull_request' && needs.pipeline.outputs.summary != ''
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
    steps:
      - name: Comment the preview on the PR
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          GH_REPO: ${{{{ github.repository }}}}
          PR: ${{{{ github.event.pull_request.number }}}}
          BODY: ${{{{ needs.pipeline.outputs.summary }}}}
        run: gh pr comment "$PR" --body "$BODY" --edit-last --create-if-none
"""


def render_workflow(source: str, python: str = ".venv/bin/python", branch: str = "main") -> str:
    return WORKFLOW.format(source=source, python=python, branch=branch, setup_uv=SETUP_UV)


def write_workflow(root: Path, source: str, branch: str = "main", force: bool = False) -> Path:
    path = root / ".github" / "workflows" / "kship.yml"
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_workflow(source, branch=branch))
    return path
