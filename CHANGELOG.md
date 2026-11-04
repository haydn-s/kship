# Changelog

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
