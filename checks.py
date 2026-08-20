#!/usr/bin/env python3
"""Small, dependency-free checks for LTspice scalar and vector results."""

from __future__ import annotations

import math
from collections.abc import Iterable


def assert_close(name: str, actual: float, expected: float, tolerance: float) -> None:
    if not math.isfinite(actual) or abs(actual - expected) > tolerance:
        raise AssertionError(
            f"{name}: expected {expected} +/- {tolerance}, got {actual}"
        )


def assert_between(name: str, actual: float, minimum: float, maximum: float) -> None:
    if not math.isfinite(actual) or not minimum <= actual <= maximum:
        raise AssertionError(
            f"{name}: expected {minimum} <= value <= {maximum}, got {actual}"
        )


def real_values(values: Iterable[float | complex]) -> list[float]:
    return [float(value.real if isinstance(value, complex) else value) for value in values]


def peak(values: Iterable[float | complex]) -> float:
    numbers = real_values(values)
    if not numbers:
        raise ValueError("Cannot find a peak in an empty vector")
    return max(numbers)


def floor(values: Iterable[float | complex]) -> float:
    numbers = real_values(values)
    if not numbers:
        raise ValueError("Cannot find a minimum in an empty vector")
    return min(numbers)
