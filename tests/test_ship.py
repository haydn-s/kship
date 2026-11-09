"""The shipping rules on their own: no Kaggle, no files, just inputs and verdicts."""

from pathlib import Path

from kship.config import Config
from kship.kapi import Limits
from kship.ship import judge_duplicate, judge_gate, judge_quota


def cfg(**kw) -> Config:
    return Config(competition="demo", root=Path("."), **kw)


def best(score, n=1):
    return score, {"n": n}


# -- the CV gate ----------------------------------------------------------------


def test_gate_always_never_stops():
    assert judge_gate(cfg(higher_is_better=True), None, best(0.99), "always") == (None, "")


def test_gate_higher_is_better():
    c = cfg(higher_is_better=True)
    assert judge_gate(c, 0.80, best(0.85), "better")[0][0] == "skipped"
    assert judge_gate(c, 0.85, best(0.85), "better")[0][0] == "skipped"  # a tie isn't an improvement
    verdict, passed = judge_gate(c, 0.86, best(0.85), "better")
    assert verdict is None and passed == "cv 0.86000 beats your best 0.85000 (#1)"


def test_gate_lower_is_better():
    # RMSE-style metrics: a sign slip here would quietly ship worse models.
    c = cfg(higher_is_better=False)
    assert judge_gate(c, 0.30, best(0.25), "better")[0][0] == "skipped"
    assert judge_gate(c, 0.20, best(0.25), "better")[0] is None


def test_gate_min_improvement():
    up, down = cfg(higher_is_better=True, min_improvement=0.01), cfg(higher_is_better=False, min_improvement=0.01)
    assert judge_gate(up, 0.855, best(0.85), "better")[0][0] == "skipped"
    assert judge_gate(up, 0.861, best(0.85), "better")[0] is None
    assert judge_gate(down, 0.245, best(0.25), "better")[0][0] == "skipped"
    assert judge_gate(down, 0.239, best(0.25), "better")[0] is None


def test_gate_without_cv_direction_or_history():
    assert judge_gate(cfg(higher_is_better=True), None, best(0.8), "better")[0][0] == "skipped"
    assert judge_gate(cfg(higher_is_better=None), 0.9, None, "better")[0][0] == "failed"
    verdict, passed = judge_gate(cfg(higher_is_better=True, cv_scheme="v2"), 0.9, None, "better")
    assert verdict is None and passed == "cv 0.90000 is the first CV on record (cv_scheme 'v2')"


# -- dedupe -------------------------------------------------------------------------


def test_duplicate_by_full_hash_or_kaggle_fingerprint():
    digest = "ab" * 32
    history = [{"n": 3, "sha256": digest, "status": "scored", "public": "0.9"}]
    assert judge_duplicate(history, digest, force=False) == (
        "skipped",
        "identical to #3 (public 0.90000). Use --force to send it again.",
    )
    assert judge_duplicate([{"ref": "77", "sub": digest[:8], "status": "scored"}], digest, False)[0] == "skipped"
    assert judge_duplicate(history, digest, force=True) is None
    assert judge_duplicate([{**history[0], "status": "error"}], digest, False) is None  # rejected ones don't count


# -- quota ----------------------------------------------------------------------------


def test_quota_and_reserve():
    c = cfg(reserve=1)
    assert judge_quota(c, None, automated=True) is None  # unknown: Kaggle decides
    assert judge_quota(c, Limits(allowed_now=2, today=3, total=10), automated=True) is None
    assert judge_quota(c, Limits(1, 4, 10), automated=True)[0] == "blocked"  # the reserve slot
    assert judge_quota(c, Limits(1, 4, 10), automated=False) is None  # a person may spend it
    assert judge_quota(c, Limits(0, 5, 10), automated=False)[0] == "blocked"
    assert judge_quota(c, Limits(0, 0, 0), automated=False)[0] == "failed"  # not joined
