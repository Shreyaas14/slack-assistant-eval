import pytest

from centaur_eval import stats


def test_cluster_bootstrap_is_seeded_and_brackets_the_mean():
    vals, clusters = [0, 1, 2, 0, 0, 3, 1, 0], list("aabbcdee")
    ci = stats.cluster_bootstrap(vals, clusters)
    assert ci == stats.cluster_bootstrap(vals, clusters)
    assert ci[0] <= sum(vals) / len(vals) <= ci[1]


def test_per_class_precision_and_recall():
    pr = stats.per_class_pr([("a", "a"), ("a", "b"), ("b", "b"), ("b", "b")], ["a", "b"])
    assert (pr["a"]["recall"], pr["a"]["precision"]) == (0.5, 1.0)
    assert pr["b"]["precision"] == pytest.approx(2 / 3)


def test_f1_is_undefined_when_a_class_is_never_predicted():
    import math

    pr = stats.per_class_pr([("a", "b"), ("b", "b")], ["a", "b"])
    assert math.isnan(pr["a"]["precision"]) and math.isnan(pr["a"]["f1"])


def test_agreement_metrics():
    pairs = [("a", "a"), ("a", "b"), ("b", "b"), ("b", "b")]
    m = stats.agreement(pairs, ["a", "b"])
    assert m["accuracy"] == 0.75
    assert m["balanced_accuracy"] == pytest.approx((0.5 + 1.0) / 2)
    assert m["macro_f1"] == pytest.approx((2 / 3 + 0.8) / 2)
    assert m["kappa"] == pytest.approx(0.5)  # p_o = .75, p_e = (2*1 + 2*3) / 16 = .5
