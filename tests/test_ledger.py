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


def test_verdict_needs_four_pairs():
    recs = [
        {"n": i, "cv": c, "public": p, "status": "scored"}
        for i, (c, p) in enumerate([(0.8, 0.79), (0.82, 0.81), (0.85, 0.83)], 1)
    ]
    assert ledger.cv_lb_verdict(recs) is None
    recs.append({"n": 4, "cv": 0.9, "public": 0.88, "status": "scored"})
    assert "Trust it" in ledger.cv_lb_verdict(recs)


def test_append_numbers_and_roundtrip(tmp_path):
    p = tmp_path / "submissions.jsonl"
    ledger.append(p, {"cv": 0.1})
    r = ledger.append(p, {"cv": 0.2})
    assert r["n"] == 2 and [x["cv"] for x in ledger.load(p)] == [0.1, 0.2]


def test_markdown_escapes_pipes():
    md = ledger.render_markdown([{"n": 1, "note": "a | b", "status": "scored"}], "comp")
    assert "a \\| b" in md
