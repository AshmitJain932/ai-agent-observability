"""Small-sample statistics for comparing groups of runs (standard library only).

Methods, chosen because they are robust to outliers, make no normality assumption, and are
exact (or close to it) for the handful of recordings people actually have:

* **Median and interquartile range** describe each group.
* **Mann-Whitney U / Wilcoxon rank-sum test** compares two groups of numbers. The p-value comes
  from the exact permutation distribution of the rank sum (ties handled with mid-ranks) when the
  groups are small enough, otherwise from the normal approximation with tie and continuity
  correction. Assumes independent recordings; tests whether one group tends to have larger values.
* **Hodges-Lehmann shift** (median of all pairwise differences) estimates how much larger the
  current values are, with a distribution-free confidence interval from the Mann-Whitney
  critical value (exact for tie-free data; approximate with ties).
* **Fisher's exact test** compares two proportions (e.g. failed runs out of all runs), with
  **Wilson score intervals** for each proportion.
* **Holm-Bonferroni** adjusts p-values when several metrics are tested together.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

EXACT_MAX_TOTAL = 60  # exact permutation p-values up to this many recordings in total
Z_975 = 1.959963984540054


# --------------------------------------------------------------------------- descriptive


def quantile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (the common 'type 7' definition)."""
    if not values:
        raise ValueError("quantile of an empty sequence")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def median(values: Sequence[float]) -> float:
    return quantile(values, 0.5)


@dataclass(frozen=True)
class Summary:
    n: int
    median: float
    q1: float
    q3: float
    minimum: float
    maximum: float
    mean: float

    @property
    def iqr(self) -> float:
        return self.q3 - self.q1


def summarize(values: Sequence[float]) -> Summary:
    return Summary(
        n=len(values),
        median=median(values),
        q1=quantile(values, 0.25),
        q3=quantile(values, 0.75),
        minimum=min(values),
        maximum=max(values),
        mean=sum(values) / len(values),
    )


def far_outliers(values: Sequence[float]) -> list[int]:
    """Indexes of values beyond Tukey's far-out fences (Q1 - 3 IQR, Q3 + 3 IQR); needs >= 4 values."""
    if len(values) < 4:
        return []
    s = summarize(values)
    spread = s.iqr if s.iqr > 0 else 0.0
    low, high = s.q1 - 3 * spread, s.q3 + 3 * spread
    return [i for i, v in enumerate(values) if (v > high or v < low) and v != s.median]


# --------------------------------------------------------------------------- Mann-Whitney


@dataclass(frozen=True)
class RankTest:
    u: float  # pairs (baseline, current) with current larger, ties counting 1/2
    p_value: float
    exact: bool


def _midranks(pooled: Sequence[float]) -> list[float]:
    order = sorted(range(len(pooled)), key=lambda i: pooled[i])
    ranks = [0.0] * len(pooled)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and pooled[order[j + 1]] == pooled[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _rank_sum_distribution(doubled_ranks: Sequence[int], k: int) -> dict[int, int]:
    """Number of size-k subsets of the pooled (doubled, integer) ranks for each possible sum."""
    # dp[j] maps subset sum -> count for subsets of size j drawn from the items seen so far.
    dp: list[dict[int, int]] = [dict() for _ in range(k + 1)]
    dp[0][0] = 1
    for rank in doubled_ranks:
        for j in range(min(k, len(dp) - 1), 0, -1):
            source = dp[j - 1]
            if not source:
                continue
            target = dp[j]
            for total, count in source.items():
                target[total + rank] = target.get(total + rank, 0) + count
    return dp[k]


def mann_whitney(baseline: Sequence[float], current: Sequence[float]) -> RankTest:
    """Two-sided test that the two groups come from the same distribution."""
    n, m = len(baseline), len(current)
    if n == 0 or m == 0:
        raise ValueError("both groups need at least one value")
    pooled = [*baseline, *current]
    ranks = _midranks(pooled)
    rank_sum_current = sum(ranks[n:])
    u = rank_sum_current - m * (m + 1) / 2
    if n + m <= EXACT_MAX_TOTAL:
        doubled = [round(r * 2) for r in ranks]
        observed = sum(doubled[n:])
        distribution = _rank_sum_distribution(doubled, m)
        expected2 = m * (n + m + 1)  # expected rank sum, doubled like the ranks
        distance = abs(observed - expected2)
        extreme = sum(c for total, c in distribution.items() if abs(total - expected2) >= distance)
        return RankTest(u=u, p_value=min(1.0, extreme / math.comb(n + m, m)), exact=True)
    total = n + m
    ties: dict[float, int] = {}
    for r in ranks:
        ties[r] = ties.get(r, 0) + 1
    tie_term = sum(t**3 - t for t in ties.values()) / (total * (total - 1))
    variance = n * m / 12 * ((total + 1) - tie_term)
    if variance <= 0:
        return RankTest(u=u, p_value=1.0, exact=False)
    z = (abs(u - n * m / 2) - 0.5) / math.sqrt(variance)
    p = math.erfc(max(z, 0.0) / math.sqrt(2))
    return RankTest(u=u, p_value=min(1.0, p), exact=False)


@lru_cache(maxsize=256)
def _u_null_counts(n: int, m: int) -> tuple[int, ...]:
    """Counts of each U value (0..n*m) under the null hypothesis, tie-free (classic recurrence)."""
    # f(n, m, u) = f(n-1, m, u-m) + f(n, m-1, u)
    table: dict[tuple[int, int], list[int]] = {}
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 or j == 0:
                table[(i, j)] = [1]
                continue
            a, b = table[(i - 1, j)], table[(i, j - 1)]
            out = [0] * (i * j + 1)
            for u, c in enumerate(a):
                out[u + j] += c
            for u, c in enumerate(b):
                out[u] += c
            table[(i, j)] = out
    return tuple(table[(n, m)])


@dataclass(frozen=True)
class Shift:
    estimate: float  # Hodges-Lehmann: median of current - baseline over all pairs
    low: float | None  # ~95% confidence interval; None when the groups are too small for one
    high: float | None


def hodges_lehmann(baseline: Sequence[float], current: Sequence[float], confidence: float = 0.95) -> Shift:
    diffs = sorted(c - b for b in baseline for c in current)
    estimate = median(diffs)
    n, m = len(baseline), len(current)
    alpha_half = (1 - confidence) / 2
    if n * m <= 2500:
        counts = _u_null_counts(n, m)
        total = math.comb(n + m, n)
        cumulative = 0
        critical = -1  # largest c with P(U <= c) <= alpha/2
        for u, count in enumerate(counts):
            cumulative += count
            if cumulative / total <= alpha_half:
                critical = u
            else:
                break
    else:
        critical = math.floor(n * m / 2 - Z_975 * math.sqrt(n * m * (n + m + 1) / 12)) - 1
    if critical < 0:
        return Shift(estimate, None, None)
    k = critical + 1  # 1-based order statistic
    return Shift(estimate, diffs[k - 1], diffs[len(diffs) - k])


# --------------------------------------------------------------------------- proportions


def fisher_exact(failed_a: int, total_a: int, failed_b: int, total_b: int) -> float:
    """Two-sided Fisher exact test for failed_a/total_a versus failed_b/total_b."""
    failed = failed_a + failed_b
    total = total_a + total_b

    def probability(x: int) -> float:
        return math.comb(total_a, x) * math.comb(total_b, failed - x) / math.comb(total, failed)

    observed = probability(failed_a)
    low, high = max(0, failed - total_b), min(failed, total_a)
    p = sum(prob for x in range(low, high + 1) if (prob := probability(x)) <= observed * (1 + 1e-9))
    return min(1.0, p)


def wilson(successes: int, total: int) -> tuple[float, float]:
    """95% Wilson score interval for a proportion."""
    if total == 0:
        return (0.0, 1.0)
    p = successes / total
    z2 = Z_975**2
    centre = (p + z2 / (2 * total)) / (1 + z2 / total)
    half = Z_975 * math.sqrt(p * (1 - p) / total + z2 / (4 * total**2)) / (1 + z2 / total)
    return (max(0.0, centre - half), min(1.0, centre + half))


# --------------------------------------------------------------------------- multiple testing


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values (same order as the input)."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[index]))
        adjusted[index] = running
    return adjusted
