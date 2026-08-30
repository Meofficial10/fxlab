"""Independent scalar reference calculations for Candidate B synthetic tests.

This module deliberately does not import the production Candidate B implementation.
The Student-t goldens are the 0.95 quantiles published by R's ``qt`` reference
implementation and cross-checked against NIST distribution tables where tabulated.
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from fractions import Fraction

import numpy as np

STUDENT_T_95_GOLDENS = {
    1: 6.313751514675037,
    2: 2.919985580353724,
    5: 2.015048373333023,
    10: 1.812461122810734,
    22: 1.717144374380242,
    30: 1.697260886593958,
    82: 1.663649184030336,
    1_000_000: 1.644855150722029,
}


def normalized_return(start: Decimal, end: Decimal, *, inverse: bool) -> Decimal:
    if start <= 0 or end <= 0:
        raise ValueError("prices must be positive")
    start_value = Decimal(1) / start if inverse else start
    end_value = Decimal(1) / end if inverse else end
    return end_value / start_value - Decimal(1)


def ranked_currencies(differentials: dict[str, Decimal]) -> tuple[str, ...]:
    return tuple(sorted(differentials, key=lambda item: (-differentials[item], item)))


def frozen_weights(differentials: dict[str, Decimal]) -> dict[str, Decimal]:
    ordered = ranked_currencies(differentials)
    result = {currency: Decimal(0) for currency in ordered}
    for currency in ordered[:2]:
        result[currency] = Decimal("0.25")
    for currency in ordered[-2:]:
        result[currency] = Decimal("-0.25")
    return result


def hac_fraction_goldens() -> dict[str, Fraction | tuple[Fraction, ...]]:
    return {
        "mean": Fraction(1, 125),
        "gammas": (
            Fraction(37, 125000),
            Fraction(-59, 312500),
            Fraction(133, 1250000),
            Fraction(-11, 156250),
        ),
        "weights": (Fraction(3, 4), Fraction(1, 2), Fraction(1, 4)),
        "lrv": Fraction(21, 250000),
        "variance_of_mean": Fraction(21, 1250000),
    }


def circular_bootstrap_reference(values: tuple[float, ...]) -> tuple[float, str]:
    """Scalar-loop oracle for the frozen circular moving-block bootstrap."""
    n = len(values)
    rng = np.random.Generator(np.random.PCG64(20260829))
    starts_per_replication = math.ceil(n / 3)
    means: list[float] = []
    complete_indices: list[int] = []
    for _ in range(10_000):
        sample: list[float] = []
        indices: list[int] = []
        for start in rng.integers(0, n, size=starts_per_replication):
            for offset in range(3):
                index = (int(start) + offset) % n
                indices.append(index)
                sample.append(values[index])
        complete_indices.extend(indices[:n])
        means.append(sum(sample[:n]) / n)
    encoded = np.asarray(complete_indices, dtype="<i8").tobytes(order="C")
    digest = hashlib.sha256(encoded).hexdigest()
    return float(np.quantile(np.asarray(means), 0.05, method="linear")), digest
