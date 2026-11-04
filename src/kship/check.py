"""Validate a submission file against the competition's sample submission.

A malformed file still burns one of your daily submissions, so everything that
can be caught locally is caught here: header, row count, the exact ID set,
duplicates, unparseable or non-finite numbers, and an optional value range.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import math
from dataclasses import dataclass, field
from pathlib import Path

SHOWN = 3  # example values quoted per problem


@dataclass
class CheckResult:
    path: Path
    rows: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    against_sample: bool = False  # True when the header, row count and IDs were compared to a sample

    @property
    def ok(self) -> bool:
        return not self.errors


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _open_text(path: Path) -> io.TextIOBase:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", newline="", encoding="utf-8-sig")
    return open(path, newline="", encoding="utf-8-sig")


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _examples(items) -> str:
    items = list(items)
    shown = ", ".join(repr(x) for x in items[:SHOWN])
    return shown + (f" (+{len(items) - SHOWN} more)" if len(items) > SHOWN else "")


def _read_sample(sample: Path):
    with _open_text(sample) as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return None, [], []
        ids, numeric = [], [True] * (len(header) - 1)
        for row in reader:
            if not row:
                continue
            ids.append(row[0])
            for j, v in enumerate(row[1:]):
                if j < len(numeric) and numeric[j] and v != "" and not _is_number(v):
                    numeric[j] = False
        return header, ids, numeric


def check_submission(
    path: Path,
    sample: Path | None = None,
    value_range: tuple[float, float] | None = None,
) -> CheckResult:
    res = CheckResult(path=path)
    if not path.is_file():
        res.errors.append(f"no such file: {path}")
        return res
    if path.suffix == ".zip":
        res.warnings.append("zip files aren't inspected; only the upload itself will validate it")
        return res

    s_header, s_ids, s_numeric = (None, [], [])
    if sample is None:
        res.warnings.append(
            "no sample submission to compare against (set sample_submission in kship.toml), "
            "so the header, row count and IDs weren't checked"
        )
    elif sample.is_file():
        s_header, s_ids, s_numeric = _read_sample(sample)
        res.against_sample = s_header is not None
    else:
        res.warnings.append(f"sample submission {sample} not found, so the header, row count and IDs weren't checked")

    with _open_text(path) as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            res.errors.append("file is empty")
            return res
        header = [h.strip() for h in header]

        if s_header is not None:
            if header != [h.strip() for h in s_header]:
                res.errors.append(f"header is {','.join(header)} but the sample has {','.join(s_header)}")
                return res
            numeric = s_numeric
        else:
            numeric = [None] * (len(header) - 1)  # infer as we go

        width = len(header)
        seen: dict[str, int] = {}
        dupes: list[str] = []
        bad_width: list[int] = []
        bad_value: list[str] = []
        out_of_range: list[str] = []
        order: list[str] = []

        for lineno, row in enumerate(reader, start=2):
            if not row:
                continue
            res.rows += 1
            if len(row) != width:
                bad_width.append(lineno)
                continue
            rid = row[0]
            if rid in seen:
                dupes.append(rid)
            seen[rid] = lineno
            if s_ids:
                order.append(rid)
            for j, v in enumerate(row[1:]):
                col = header[j + 1]
                if numeric[j] is None:
                    numeric[j] = _is_number(v)
                if v.strip() == "":
                    bad_value.append(f"line {lineno} {col}=<empty>")
                elif numeric[j]:
                    try:
                        x = float(v)
                    except ValueError:
                        bad_value.append(f"line {lineno} {col}={v}")
                        continue
                    if not math.isfinite(x):
                        bad_value.append(f"line {lineno} {col}={v}")
                    elif value_range and not (value_range[0] <= x <= value_range[1]):
                        out_of_range.append(f"line {lineno} {col}={v}")

    if res.rows == 0:
        res.errors.append("file has a header but no rows")
    if bad_width:
        res.errors.append(f"{len(bad_width)} rows don't have {width} columns, e.g. lines {_examples(bad_width)}")
    if dupes:
        res.errors.append(f"{len(dupes)} duplicate IDs: {_examples(dupes)}")
    if bad_value:
        res.errors.append(f"{len(bad_value)} empty or non-numeric values: {_examples(bad_value)}")
    if out_of_range:
        lo, hi = value_range
        res.errors.append(f"{len(out_of_range)} values outside [{lo:g}, {hi:g}]: {_examples(out_of_range)}")

    if s_ids:
        sample_set = set(s_ids)
        missing = [i for i in s_ids if i not in seen]
        extra = [i for i in seen if i not in sample_set]
        if res.rows != len(s_ids):
            res.errors.append(f"{res.rows} rows but the sample has {len(s_ids)}")
        if missing:
            res.errors.append(f"{len(missing)} IDs from the sample are missing: {_examples(missing)}")
        if extra:
            res.errors.append(f"{len(extra)} IDs aren't in the sample: {_examples(extra)}")
        if not missing and not extra and not dupes and order != s_ids:
            res.warnings.append("rows are in a different order than the sample (usually fine, Kaggle joins on ID)")

    return res
