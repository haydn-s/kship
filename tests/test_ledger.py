from kship import ledger


def test_spearman_perfect_and_inverse():
    assert ledger.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert ledger.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert ledger.spearman([1, 1, 1], [1, 2, 3]) is None


def test_best_ignores_errors_and_respects_direction():
    recs = [
        {"n": 1, "cv": 0.8, "status": "scored"},
        {"n": 2, "cv": 0.95, "status": "error"},
        {"n": 3, "cv": 0.85, "status": "scored"},
    ]
    assert ledger.best(recs, "cv", higher=True)[1]["n"] == 3
    assert ledger.best(recs, "cv", higher=False)[1]["n"] == 1


def test_improves_with_margin():
    assert ledger.improves(0.91, 0.90, True)
    assert not ledger.improves(0.905, 0.90, True, margin=0.01)
    assert ledger.improves(0.5, 0.6, False)


def test_verdict_needs_eight_pairs():
    # With 4 pairs, shuffled scores reach rho >= 0.7 one time in 6: too often to say "trust it".
    recs = [{"n": i, "cv": 0.8 + i / 100, "public": 0.79 + i / 100, "status": "scored"} for i in range(1, 8)]
    assert ledger.cv_lb_verdict(recs) is None
    recs.append({"n": 8, "cv": 0.9, "public": 0.89, "status": "scored"})
    assert "trust it" in ledger.cv_lb_verdict(recs)


def test_verdict_in_markdown_only_uses_the_current_scheme():
    old = [{"n": i, "cv": 0.9 - i / 100, "public": 0.7 + i / 100, "status": "scored"} for i in range(1, 9)]
    new = [
        {"n": 8 + i, "cv": 0.8 + i / 100, "public": 0.8 + i / 100, "status": "scored", "cv_scheme": "v2"}
        for i in range(1, 9)
    ]
    assert "disagree" in ledger.render_markdown(old + new, "comp")
    assert "trust it" in ledger.render_markdown(old + new, "comp", cv_scheme="v2")


def test_append_numbers_and_roundtrip(tmp_path):
    p = tmp_path / "submissions.jsonl"
    ledger.append(p, {"cv": 0.1})
    r = ledger.append(p, {"cv": 0.2})
    assert r["n"] == 2 and [x["cv"] for x in ledger.load(p)] == [0.1, 0.2]


def test_markdown_escapes_pipes():
    md = ledger.render_markdown([{"n": 1, "note": "a | b", "status": "scored"}], "comp")
    assert "a \\| b" in md
