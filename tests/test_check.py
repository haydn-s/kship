import gzip

from kship.check import check_submission


def make(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


SAMPLE = "id,target\n1,0\n2,0\n3,0\n"


def run(tmp_path, text, **kw):
    sample = make(tmp_path, "sample.csv", SAMPLE)
    return check_submission(make(tmp_path, "sub.csv", text), sample, **kw)


def test_valid_file_passes(tmp_path):
    res = run(tmp_path, "id,target\n1,0.1\n2,0.9\n3,0.5\n")
    assert res.ok and res.rows == 3 and not res.warnings


def test_header_mismatch(tmp_path):
    res = run(tmp_path, "ID,Target\n1,0.1\n2,0.9\n3,0.5\n")
    assert not res.ok and "header" in res.errors[0]


def test_missing_extra_and_duplicate_ids(tmp_path):
    res = run(tmp_path, "id,target\n1,0.1\n1,0.2\n4,0.5\n")
    text = " | ".join(res.errors)
    assert "duplicate" in text and "missing" in text and "aren't in the sample" in text


def test_nan_empty_and_text_values(tmp_path):
    res = run(tmp_path, "id,target\n1,nan\n2,\n3,abc\n")
    assert not res.ok and "3 empty or non-numeric" in res.errors[0]


def test_value_range(tmp_path):
    res = run(tmp_path, "id,target\n1,0.1\n2,1.5\n3,-0.2\n", value_range=(0, 1))
    assert not res.ok and "2 values outside [0, 1]" in res.errors[0]


def test_reordered_rows_is_only_a_warning(tmp_path):
    res = run(tmp_path, "id,target\n3,0.1\n1,0.9\n2,0.5\n")
    assert res.ok and "different order" in res.warnings[0]


def test_ragged_rows(tmp_path):
    res = run(tmp_path, "id,target\n1,0.1,9\n2,0.9\n3,0.5\n")
    assert not res.ok and "don't have 2 columns" in res.errors[0]


def test_string_labels_allowed_when_sample_is_text(tmp_path):
    sample = make(tmp_path, "sample.csv", "id,label\n1,cat\n2,cat\n")
    res = check_submission(make(tmp_path, "s.csv", "id,label\n1,dog\n2,cat\n"), sample)
    assert res.ok


def test_gzip_and_bom(tmp_path):
    sample = make(tmp_path, "sample.csv", SAMPLE)
    p = tmp_path / "sub.csv.gz"
    with gzip.open(p, "wt") as f:
        f.write("﻿id,target\n1,0.1\n2,0.2\n3,0.3\n")
    assert check_submission(p, sample).ok


def test_without_sample_still_catches_basics(tmp_path):
    res = check_submission(make(tmp_path, "s.csv", "id,y\n1,0.2\n2,inf\n"), None)
    assert not res.ok


def test_without_sample_says_what_was_not_checked(tmp_path):
    res = check_submission(make(tmp_path, "s.csv", "id,y\n1,0.2\n"), None)
    assert res.ok and not res.against_sample and "IDs weren't checked" in res.warnings[0]
    missing = check_submission(make(tmp_path, "t.csv", "id,y\n1,0.2\n"), tmp_path / "nope.csv")
    assert not missing.against_sample and "IDs weren't checked" in missing.warnings[0]


def test_with_sample_is_marked_as_compared(tmp_path):
    assert run(tmp_path, "id,target\n1,0.1\n2,0.9\n3,0.5\n").against_sample
