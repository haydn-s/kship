# Changelog

## Unreleased

- `kship init` / `kship new` find the sample submission when the competition keeps its files in a folder (e.g. `for_students/sample_submission.csv`, common in class competitions) or serves it zipped. Before, `sample_submission` was left empty, which quietly switched off the row-count and ID checks
- `kship check` no longer says a file "matches the sample submission" when there was no sample to compare against; it warns that the header, row count and IDs weren't checked
- A failed sample download says why (e.g. 403: rules not accepted) instead of always blaming the rules
- CI: merges note the PR title on Kaggle ("LightGBM + TE (#7)") instead of "Merge pull request #7 from ...". `kship run -m` takes a whole commit message and uses its subject line. Regenerate the workflow with `kship ci --force` to pick this up
- `kship run` output stays in order when piped (CI logs showed the pipeline's output above kship's own header)

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
