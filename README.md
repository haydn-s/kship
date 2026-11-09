# kship

[![test](https://github.com/haydn-s/kship/actions/workflows/test.yml/badge.svg)](https://github.com/haydn-s/kship/actions/workflows/test.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-blue)

Ship Kaggle submissions straight from your pipeline, without burning attempts on mistakes.

```sh
make ship MSG="lightgbm + target encoding"
```

runs your pipeline, and only if it succeeds:

1. **validates** the new CSV against the sample submission: header, row count, the exact ID set, duplicates, NaN/inf, optional value range
2. **skips duplicates**: the same predictions (by SHA-256) are never uploaded twice
3. **gates on CV**: ships only if your cross-validated score beats your best so far
4. **guards the quota**: automated runs leave `reserve` daily submissions untouched for you
5. **uploads** (a CSV, or for code competitions it pushes `kernel/`, waits for it to run, then submits that version)
6. **waits for the leaderboard score**, prints the delta against your best, and sends a macOS notification
7. **logs it** to `submissions.jsonl` with the commit hash, file hash, CV and scores

## Install (once per machine)

```sh
git clone https://github.com/haydn-s/kship kship && cd kship
./bootstrap.sh
```

That installs `kaggle` and `kship` as global tools (via `uv tool`, falling back to `pipx`), signs you in if you have no credentials (paste a token, or press Enter for browser sign-in), and runs `kship doctor`.

Pasting a token is the better choice if you'll use CI: the same token becomes the repo secret with `gh secret set KAGGLE_API_TOKEN < ~/.kaggle/access_token`. Already signed in through the browser? Generate a token at kaggle.com/settings/api and save it alongside; the token file takes precedence:

```sh
pbpaste > ~/.kaggle/access_token && chmod 600 ~/.kaggle/access_token
```

Just want the CLI, without cloning? `uv tool install git+https://github.com/haydn-s/kship --with six` (then sign in with `kaggle auth login` or a token).

## Start a competition

```sh
kship new playground-series-s6e11        # folder, git repo, kship.toml, Makefile, src/pipeline.py
cd playground-series-s6e11
make setup && make data
```

Accept the competition's rules on Kaggle first, or the API returns 403s. `kship doctor` tells you if you haven't.

For an existing project, `kship init <slug>` adds just the `kship.toml`.

## Wire it into your pipeline

Your pipeline writes its CSV into `submissions/` and prints marker lines:

```python
print(f"KSHIP_FILE={path}")   # optional: otherwise the newest new file in submissions/ is used
print(f"KSHIP_CV={cv:.6f}")   # your cross-validated score, used by the gate
print("KSHIP_MSG=lightgbm + target encoding")   # optional: the note, if `-m` doesn't give one
```

Markers have to be on **stdout**, each on a line of its own (leading and trailing spaces are
fine). kship doesn't read stderr, which is where Python's `logging` writes by default, so
`logging.info(f"KSHIP_CV={cv}")` won't be seen; use `print`. If a marker appears more than
once, the last one wins. `-m` on the command line beats `KSHIP_MSG`.

With no `KSHIP_CV` line there's nothing for the gate to compare, so `kship run` doesn't ship
(it says why). Pass `--gate always` to ship anyway, e.g. for a first baseline.

Then any command can be shipped:

```sh
kship run -- python -m src.pipeline
kship run -m "deeper trees" -- python train.py --depth 12
kship run --gate always -- ./train.sh       # ship even if CV didn't improve
```

The markers work from any language and any tool: notebooks via `jupyter nbconvert --execute`, shell scripts, R.

## Commands

| command | what it does |
|---|---|
| `kship doctor [-c slug]` | CLI, credentials (and their file permissions), sign-in, competition joined, submissions left today |
| `kship new <slug> [dir]` | scaffold a ship-ready project |
| `kship init <slug>` | add `kship.toml` to the current project, fetch the sample submission |
| `kship ci` | add the GitHub Actions workflow (`kship new` includes it) |
| `kship data` | download and unzip all competition files into `data/` |
| `kship check [file]` | validate without uploading |
| `kship submit [file] [-m] [--cv]` | validate, dedupe, upload now (no CV gate, no reserve) |
| `kship run [-m] [--cv] -- <cmd>` | run a pipeline, then ship its output if it improved |
| `kship log [--refresh] [--md]` | history with scores, best runs, CV↔leaderboard rank agreement |

`--dry-run` runs every check without uploading. `--no-wait` skips the score polling.

## CI/CD: ship on merge

`kship new` (or `kship ci` in an existing project) adds `.github/workflows/kship.yml`:

- **pull request**: installs, downloads data (cached), runs the pipeline with `--dry-run`, and comments on the PR whether it *would* ship, with this run's CV next to your best
- **merge to main**: runs the pipeline again and ships it if the CV beats your best. The job summary shows the decision, CV, and the public score once Kaggle returns it. The submission's note is the commit's subject line, or the PR title for "Merge pull request #N" commits
- serialized per branch, so two quick merges can't race each other for the same "best". GitHub keeps only the newest waiting run, so with three quick merges the middle one is cancelled; the newest run includes its commit
- `KAGGLE_API_TOKEN` reaches only the steps that run kship, not `make setup` (installing dependencies). The PR comment is posted by a separate job, the only one that can write to the PR
- each run keeps the file kship judged and `submissions.jsonl` as an artifact for 30 days

**No shared state needed.** Every submission message is tagged, e.g.
`lightgbm + TE · cv=0.88120 · git=a1b2c3d · sub=5fa03265`. kship reads those tags back from Kaggle's
own submission list, so a fresh CI runner gates against submissions made from your laptop and vice
versa, and the `sub=` fingerprint stops the same predictions from being uploaded twice anywhere.
`kship log --refresh` pulls CI's submissions into your local ledger.

`submissions.jsonl` is meant to be committed. `kship new` adds `submissions.jsonl merge=union` to
`.gitattributes`, so lines added on two machines merge without a conflict; in an existing project,
add that line yourself. Changes to the ledger and `SUBMISSIONS.md` don't mark a submission's
commit as dirty.

One-time per competition repo:

```sh
gh repo create <you>/<competition-slug> --private --source . --push
gh secret set KAGGLE_API_TOKEN < ~/.kaggle/access_token   # or paste one from kaggle.com/settings/api
```

The workflow installs kship from `KSHIP_SOURCE` at the top of the file. `kship ci` fills that in
for you: if kship was installed from git, it reuses that exact URL and revision; otherwise it
uses this repo's `Repository` URL from `pyproject.toml`, pinned to the release tag (e.g.
`git+https://github.com/haydn-s/kship@v0.1.1`). Set `KSHIP_SOURCE` or pass `--source` to override.

The pipeline runs on a standard GitHub runner (2–4 CPUs, no GPU, 120-minute limit set in the
workflow). For heavier training, point `runs-on` at a self-hosted runner, or use a code
competition's notebook mode so the training happens on Kaggle.

### Exit codes

| code | meaning |
|---|---|
| 0 | shipped, would ship (dry run), or deliberately skipped (not better / duplicate) |
| 1 | failed: pipeline error, invalid file, Kaggle rejected or couldn't score it |
| 3 | blocked by the daily limit or `reserve`; the workflow treats this as a warning, re-run it tomorrow |

In GitHub Actions kship also writes step outputs (`decision`, `shipped`, `cv`, `best_cv`, `public`)
for later steps to use.

## CV↔LB agreement

Once eight or more submissions have both a CV and a public score, `kship log` reports the Spearman rank correlation between them. High means your CV ranks models the way the leaderboard does, so trust it. Low means the public split is noisy or your CV leaks, so stop tuning against the public score. (Fewer than eight pairs is too few: with four, shuffled scores look like strong agreement one time in six.)

## Changing how you compute CV

The gate compares your CV with your best so far, which only makes sense if both were computed the
same way. When you change the CV itself (more folds, a different split, a fixed leak), set a new
`cv_scheme` in `kship.toml`:

```toml
cv_scheme = "v2"
```

Submissions are then tagged `cvs=v2` on Kaggle, and the gate, `kship log`'s best CV and the CV↔LB
correlation only look at submissions from the same scheme. The default, `""`, matches every
submission made before you set one.

## kship.toml

```toml
competition = "playground-series-s6e11"
mode = "file"                 # "notebook" for code competitions
submission_dir = "submissions"
sample_submission = "data/sample_submission.csv"
metric = "AUC"
higher_is_better = true       # guessed from the metric name; check it
min_improvement = 0           # CV must beat your best by more than this
cv_scheme = ""                # change it when you change how CV is computed (see above)
reserve = 1                   # daily slots `kship run` won't spend
poll_minutes = 10

[check]
range = [0, 1]                # optional

[notebook]
kernel_dir = "kernel"
output_file = "submission.csv"
timeout_minutes = 120
```

## Credentials

kship uses the official `kaggle` package (2.2+), which looks for credentials in this order: `KAGGLE_API_TOKEN` env var, `~/.kaggle/access_token`, legacy `~/.kaggle/kaggle.json`, then the OAuth login saved by `kaggle auth login`. `kship doctor` shows which one is in use.

Heads-up: kaggle 2.2.4 and 2.2.5 import `six` without declaring it, so a clean `pip install kaggle` crashes on launch. kship depends on `six` and `bootstrap.sh` installs it next to the CLI.

## Development

```sh
uv venv && uv pip install -e '.[test]'
.venv/bin/pytest                       # a few seconds
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
```

The tests run against a fake Kaggle API, plus a check that the real `KaggleApi` still has the
method signatures kship calls, and an `actionlint` pass over the generated workflow. CI runs
them on Python 3.11 to 3.14 (Linux) and 3.14 (macOS).

`main` is protected by the ruleset in `.github/rulesets/main.json`: changes land through pull
requests, the `checks-passed` job (lint plus every test job) has to pass first, and no one can
force-push or delete `main`, admins included. `./protect-main.sh` applies it to the GitHub repo,
and re-running it after you edit the JSON updates the live ruleset.

### Releasing

1. In a pull request, bump `version` in `pyproject.toml` and `src/kship/__init__.py`, and turn
   the changelog's "Unreleased" heading into `## <version> (<date>)`.
2. Once it's merged, tag `main` and push the tag: `git tag v<version> && git push origin v<version>`.

The tag runs `.github/workflows/release.yml`. It tests and builds kship, fails if the tag doesn't
match the built version or the changelog has no section for it, and creates the GitHub release
with that section as its notes. Push the tag before anyone runs `kship ci` with the new version:
generated workflows install from that tag, and `kship ci` warns when it isn't on GitHub yet.

**Publishing to PyPI** is off until you set it up once:

1. On pypi.org, add a "pending publisher" for project `kship`: this repository, workflow
   `release.yml`, environment `pypi`.
2. In this repo's settings, create the `pypi` environment, then add a repository variable
   `PUBLISH_TO_PYPI` set to `true`.

After that, each tag also publishes to PyPI through trusted publishing (no token is stored).
Copies of kship installed from PyPI point generated workflows at `kship==<version>` instead of
a git tag.

### Layout

| path | what's in it |
|---|---|
| `src/kship/cli.py` | commands, and `ship()`: validate → dedupe → gate → quota → upload → score |
| `src/kship/kapi.py` | thin wrapper over the official Kaggle client; credential discovery; error translation |
| `src/kship/check.py` | submission validation against the sample |
| `src/kship/ledger.py` | `submissions.jsonl`, reading tags back from Kaggle, CV↔LB correlation |
| `src/kship/ci.py` | GitHub Actions outputs, job summary, the workflow template |
| `src/kship/config.py` | `kship.toml` |
| `src/kship/scaffold.py` | files `kship new` creates |
| `bootstrap.sh` | one-time machine setup |
| `protect-main.sh` | applies `main`'s ruleset (`.github/rulesets/main.json`) on GitHub |
| `.github/workflows/release.yml` | on a `v*` tag: test, build, GitHub release, and PyPI once switched on |

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
