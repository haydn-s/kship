# Changelog

## Unreleased

- Submitting no longer marks your commit dirty (`git=a1b2c3d*`, "uncommitted changes") just because the last submission added a line to `submissions.jsonl`. The ledger and `SUBMISSIONS.md` don't count as uncommitted changes; everything else still does
- A `sample_submission` that's set in `kship.toml` but missing on disk is now an error. Before, the header, row count and ID checks were skipped with a warning, and `kship run --dry-run` said a file with the wrong IDs "passes every check". This could happen on a CI runner, because `kship data` keeps the archive's folders (`data/for_students/sample_submission.csv`) while `kship init` saves the sample as `data/sample_submission.csv`. Now `kship data`, `kship check` and shipping copy the sample to where `kship.toml` expects it when there's exactly one file by that name under `data/`
- History reads every page of your Kaggle submissions, not just the newest 200. Past 200, older best CVs and fingerprints had stopped counting, so the gate could ship a worse CV and the duplicate check could upload the same file again
- **Behaviour change:** `kship run` (and `--gate better`) no longer ships when there's no CV score; it says why and exits 0. A pipeline that stopped printing `KSHIP_CV` (or started printing it to stderr) would otherwise spend a submission on every merge. Use `--gate always` (`make force-ship`) to ship without one
- New `cv_scheme` setting in `kship.toml`. Change it when you change how CV is computed: submissions are tagged `cvs=<scheme>`, and the gate, the best CV in `kship log`, and the CV↔LB correlation only compare within one scheme. Before, one leaky CV could block the gate for good
- The CV↔LB verdict needs 8 submissions with both scores, not 4. With 4, random scores look like strong agreement one time in six
- `submissions.jsonl` copes with git merges: `kship new` adds `submissions.jsonl merge=union` to `.gitattributes`, unresolved conflict markers get a clear message saying how to fix them (instead of a JSONDecodeError), and new records are numbered after the highest number rather than after the last line

## 0.1.1 (2026-11-08)

Upgrading: reinstall kship (`uv tool install --force 'git+https://github.com/haydn-s/kship@v0.1.1' --with six`, or `git pull` in an editable clone), then run `kship ci --force` in each competition repo so its workflow installs 0.1.1. That overwrites any edits you made to `.github/workflows/kship.yml`.

- `kship init` / `kship new` find the sample submission when the competition keeps its files in a folder (e.g. `for_students/sample_submission.csv`, common in class competitions) or serves it zipped. Before, `sample_submission` was left empty, which quietly switched off the row-count and ID checks
- `kship check` no longer says a file "matches the sample submission" when there was no sample to compare against; it warns that the header, row count and IDs weren't checked
- A failed sample download says why (e.g. 403: rules not accepted) instead of always blaming the rules
- CI: merges note the PR title on Kaggle ("LightGBM + TE (#7)") instead of "Merge pull request #7 from ...". `kship run -m` takes a whole commit message and uses its subject line. Regenerate the workflow with `kship ci --force` to pick this up
- `kship run` output stays in order when piped (CI logs showed the pipeline's output above kship's own header)
- This repo: `main` is protected by a ruleset kept in `.github/rulesets/main.json` (pull requests only, lint and tests must pass). `protect-main.sh` applies it, and `publish.sh` runs it after the first push

## 0.1.0 (2026-11-04)

First release.

- `kship doctor`: checks the Kaggle CLI, every credential source (and its file permissions), sign-in, whether you've joined a competition, and submissions left today
- `kship new` / `kship init`: scaffold a competition project, or add `kship.toml` to an existing one; fetches the sample submission and guesses the metric's direction
- `kship check`: validates a submission against the sample (header, row count, exact ID set, duplicates, NaN/inf, optional value range)
- `kship submit` / `kship run`: dedupe by file fingerprint, CV gate, daily-quota reserve for automated runs, upload, wait for the public score, macOS notification
- Code competitions: pushes `kernel/`, waits for the run, submits that version
- `kship log`: history with scores, best runs, and CV↔leaderboard rank correlation; `--refresh` imports submissions made elsewhere; `--md` writes `SUBMISSIONS.md`
- `kship ci`: GitHub Actions workflow that dry-runs on pull requests (with a PR comment) and ships on merge to main
- Kaggle as the source of truth: submission messages carry `cv=`, `git=` and `sub=` tags, so any machine or CI runner gates and dedupes against the same history
- Depends on `six` explicitly, because kaggle 2.2.4–2.2.5 import it without declaring it
