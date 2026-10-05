"""Small-sample statistics implemented with the standard library only."""

from __future__ import annotations

import random
from collections.abc import Sequence


def cluster_bootstrap(
    values: Sequence[float],
    clusters: Sequence[str],
    b: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile CI for the mean of `values`, resampling whole clusters (e.g. scenario families)."""
    if not values:
        return (float("nan"), float("nan"))
    groups: dict[str, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        groups.setdefault(c, []).append(v)
    keys = sorted(groups)
    rng = random.Random(seed)
    means = []
    for _ in range(b):
        sample: list[float] = []
        for _ in keys:
            sample.extend(groups[rng.choice(keys)])
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int((alpha / 2) * b)]
    hi = means[min(b - 1, int((1 - alpha / 2) * b))]
    return (lo, hi)


def per_class_pr(pairs: Sequence[tuple[str, str]], classes: Sequence[str]) -> dict[str, dict]:
    """Precision/recall/F1 per class from (gold, pred) pairs. Preds outside `classes` count as misses; a metric whose
    denominator is zero is NaN."""
    out = {}
    for c in classes:
        tp = sum(1 for g, p in pairs if g == c and p == c)
        n_gold = sum(1 for g, _ in pairs if g == c)
        n_pred = sum(1 for _, p in pairs if p == c)
        prec = tp / n_pred if n_pred else float("nan")
        rec = tp / n_gold if n_gold else float("nan")
        if not (n_pred and n_gold):
            f1 = float("nan")  # undefined when the class is never predicted or never gold
        else:
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[c] = {
            "tp": tp,
            "n_gold": n_gold,
            "n_pred": n_pred,
            "precision": prec,
            "recall": rec,
            "f1": f1,
        }
    return out
