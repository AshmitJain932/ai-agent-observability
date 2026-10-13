"""The small-sample statistics used by `aobs-server compare-groups`, checked against known values
and against brute-force enumeration."""

from __future__ import annotations

import itertools
import math
import random

import pytest

from aobs_server.diagnosis.stats import (
    far_outliers,
    fisher_exact,
    hodges_lehmann,
    holm,
    mann_whitney,
    quantile,
    summarize,
    wilson,
)


def brute_force_p(x: list[float], y: list[float]) -> float:
    """Two-sided permutation p-value of the rank-sum statistic, by enumerating every split."""
    pooled = [*x, *y]
    n = len(x)

    def stat(group: list[float], other: list[float]) -> float:
        return sum((g > o) + 0.5 * (g == o) for g in group for o in other)

    expected = n * len(y) / 2
    observed = abs(stat(y, x) - expected)
    hits = total = 0
    for idx in itertools.combinations(range(len(pooled)), len(y)):
        chosen = [pooled[i] for i in idx]
        rest = [pooled[i] for i in range(len(pooled)) if i not in idx]
        total += 1
        hits += abs(stat(chosen, rest) - expected) >= observed - 1e-9
    return hits / total


def test_quantiles_and_summary() -> None:
    assert quantile([1, 2, 3, 4], 0.5) == 2.5
    assert quantile([10], 0.25) == 10
    s = summarize([1, 2, 3, 4, 100])
    assert (s.n, s.median, s.q1, s.q3, s.minimum, s.maximum) == (5, 3, 2, 4, 1, 100)
    with pytest.raises(ValueError):
        quantile([], 0.5)


def test_far_outliers() -> None:
    assert far_outliers([100, 101, 99, 102, 98, 100, 5000]) == [6]
    assert far_outliers([100, 101, 99]) == []  # too few values to call anything an outlier
    assert far_outliers([5, 5, 5, 5]) == []


def test_mann_whitney_known_value_complete_separation() -> None:
    result = mann_whitney([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    assert result.exact and result.u == 25
    assert result.p_value == pytest.approx(2 / 252)


def test_three_versus_three_can_never_reach_significance() -> None:
    assert mann_whitney([1, 2, 3], [7, 8, 9]).p_value == pytest.approx(0.1)


def test_identical_groups_give_p_of_one() -> None:
    assert mann_whitney([5, 5, 5, 5, 5], [5, 5, 5, 5, 5]).p_value == 1.0
    assert mann_whitney([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]).p_value == pytest.approx(1.0)


@pytest.mark.parametrize("seed", range(8))
def test_exact_p_values_match_brute_force_including_ties(seed: int) -> None:
    rng = random.Random(seed)
    x = [float(rng.randint(0, 6)) for _ in range(rng.randint(3, 6))]
    y = [float(rng.randint(2, 8)) for _ in range(rng.randint(3, 6))]
    assert mann_whitney(x, y).p_value == pytest.approx(brute_force_p(x, y))


def test_large_groups_use_the_normal_approximation() -> None:
    rng = random.Random(1)
    x = [rng.gauss(100, 10) for _ in range(40)]
    y = [rng.gauss(130, 10) for _ in range(40)]
    result = mann_whitney(x, y)
    assert not result.exact and result.p_value < 1e-6
    same = mann_whitney(x, [v + 0.0 for v in x])
    assert same.p_value > 0.9


def test_hodges_lehmann_estimate_and_interval() -> None:
    shift = hodges_lehmann([1, 2, 3], [4, 5, 6])
    assert shift.estimate == 3
    assert shift.low is None and shift.high is None  # 3 vs 3 is too small for a 95% interval
    base = [10, 11, 12, 13, 14]
    shift = hodges_lehmann(base, [b + 20 for b in base])
    assert shift.estimate == 20
    # n = m = 5: the critical U is 2, so the interval is the 3rd smallest and 3rd largest difference
    diffs = sorted(c - b for b in base for c in [b + 20 for b in base])
    assert (shift.low, shift.high) == (diffs[2], diffs[-3])
    assert shift.low is not None and shift.high is not None and shift.low <= 20 <= shift.high


def test_hodges_lehmann_is_robust_to_one_outlier() -> None:
    base = [100, 102, 98, 101, 99, 100, 103, 97]
    shift = hodges_lehmann(base, [100, 101, 99, 102, 98, 100, 97, 5000])
    assert abs(shift.estimate) < 3


def test_fisher_known_value() -> None:
    assert fisher_exact(3, 4, 1, 4) == pytest.approx(0.4857142857)  # Fisher's tea-tasting table
    assert fisher_exact(0, 10, 7, 10) == pytest.approx(0.0030959752)
    assert fisher_exact(2, 10, 2, 10) == pytest.approx(1.0)


def test_wilson_interval() -> None:
    low, high = wilson(0, 10)
    assert low == 0 and high == pytest.approx(0.2775, abs=1e-4)
    low, high = wilson(5, 10)
    assert low == pytest.approx(0.2366, abs=1e-4) and high == pytest.approx(0.7634, abs=1e-4)


def test_holm_adjustment() -> None:
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm([]) == []
    assert holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])
    assert math.isclose(holm([0.001])[0], 0.001)
