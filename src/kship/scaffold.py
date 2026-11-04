"""Files `kship new` lays down for a fresh competition project."""

from __future__ import annotations

import json
from pathlib import Path

GITIGNORE = """\
# competition data stays on Kaggle; `make data` fetches it
data/
# submission CSVs are reproducible from the ledger's commit hashes
submissions/*.csv
submissions/*.csv.gz
.venv/
__pycache__/
*.pyc
.ipynb_checkpoints/
.DS_Store
"""

MAKEFILE = """\
# `make ship` runs the pipeline and, if it succeeds, validates the new submission,
# checks your CV beat your best, and uploads it to Kaggle. MSG adds a note.
PY  ?= .venv/bin/python
MSG ?=

.PHONY: setup data train check preview ship force-ship log

setup:
\tuv venv .venv && uv pip install --python $(PY) -r requirements.txt

data:
\tkship data

train:
\t$(PY) -m src.pipeline

check:
\tkship check

preview:
\tkship run --dry-run -- $(PY) -m src.pipeline

ship:
\tkship run -m "$(MSG)" -- $(PY) -m src.pipeline

force-ship:
\tkship run --gate always -m "$(MSG)" -- $(PY) -m src.pipeline

log:
\tkship log --refresh --md
"""

REQUIREMENTS = """\
numpy
pandas
scikit-learn
"""

PIPELINE = '''\
"""Load data, cross-validate, predict the test set, write a submission.

`make ship` runs this file. It tells kship what happened through two printed lines:
    KSHIP_FILE=<path to the CSV it wrote>
    KSHIP_CV=<your cross-validated score>
kship ships the file only if the run exits cleanly and the CV beats your best.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "submissions"
SAMPLE = ROOT / "{sample}"


def main() -> None:
    sample = pd.read_csv(SAMPLE)
    id_col, *target_cols = sample.columns

    # train = pd.read_csv(DATA / "train.csv")
    # test = pd.read_csv(DATA / "test.csv")
    # cv = cross_val_score(model, X, y, cv=5, scoring=...).mean()
    cv = None

    preds = sample.copy()  # replace preds[target_cols] with your model's predictions

    OUT.mkdir(exist_ok=True)
    path = OUT / f"{{datetime.now():%Y%m%d-%H%M%S}}.csv"
    preds.to_csv(path, index=False)
    print(f"KSHIP_FILE={{path}}")
    if cv is not None:
        print(f"KSHIP_CV={{cv:.6f}}")


if __name__ == "__main__":
    main()
'''

README = """\
# {title}

Kaggle competition: <https://www.kaggle.com/competitions/{slug}>

```sh
make setup        # create .venv and install requirements
make data         # download the competition files into data/
make train        # run the pipeline without submitting
make preview      # run it and say whether it would ship, without uploading
make ship         # run it, and ship the result if CV improved
make log          # leaderboard scores for every submission, refreshed
```

CI (`.github/workflows/kship.yml`): every pull request runs the pipeline as a dry run and
comments whether it would ship; merging to `main` ships it if CV beats the best submission
on Kaggle. Needs the `KAGGLE_API_TOKEN` repo secret.

Submission history lives in `submissions.jsonl` (committed) and `SUBMISSIONS.md`.
"""

KERNEL_SCRIPT = '''\
"""Runs on Kaggle for a code competition. Read inputs from /kaggle/input, write
the predictions to /kaggle/working/{output}."""

from pathlib import Path

import pandas as pd

INPUT = Path("/kaggle/input/{slug}")
sample = next(INPUT.rglob("sample*submission*.csv"))
preds = pd.read_csv(sample)  # replace with your model's predictions
preds.to_csv("/kaggle/working/{output}", index=False)
'''


def kernel_metadata(username: str, slug: str) -> str:
    name = f"{slug[:39]}-submission"
    return (
        json.dumps(
            {
                "id": f"{username}/{name}",
                "title": name,
                "code_file": "submission.py",
                "language": "python",
                "kernel_type": "script",
                "is_private": True,
                "enable_gpu": False,
                "enable_tpu": False,
                "enable_internet": False,
                "dataset_sources": [],
                "competition_sources": [slug],
                "kernel_sources": [],
                "model_sources": [],
            },
            indent=2,
        )
        + "\n"
    )


def write_kernel(dest: Path, username: str, slug: str, output: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "kernel-metadata.json").write_text(kernel_metadata(username, slug))
    (dest / "submission.py").write_text(KERNEL_SCRIPT.format(slug=slug, output=output))


def write_project(dest: Path, slug: str, title: str, sample: str) -> list[Path]:
    files = {
        ".gitignore": GITIGNORE,
        "Makefile": MAKEFILE,
        "requirements.txt": REQUIREMENTS,
        "README.md": README.format(title=title or slug, slug=slug),
        "src/__init__.py": "",
        "src/pipeline.py": PIPELINE.format(sample=sample or "data/sample_submission.csv"),
        "submissions/.gitkeep": "",
    }
    written = []
    for rel, text in files.items():
        p = dest / rel
        if p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        written.append(p)
    return written
