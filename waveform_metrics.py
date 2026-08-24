#!/usr/bin/env python3
"""Dependency-free, full-resolution waveform measurements and requirements."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict, get_args


Number = float | complex
MetricName = Literal[
    "minimum",
    "maximum",
    "mean",
    "rms",
    "peak_to_peak",
    "rise_time",
    "overshoot",
    "settling_time",
]
ComparisonOperator = Literal["<", "<=", ">", ">="]


class WaveformRequirement(TypedDict):
    metric: MetricName
    operator: ComparisonOperator
    target: float
    initial_value: NotRequired[float]
    final_value: NotRequired[float]
    low_fraction: NotRequired[float]
    high_fraction: NotRequired[float]
    settling_tolerance: NotRequired[float]


class RequirementThreshold(TypedDict):
    operator: str
    target: float
    unit: str


class RequirementResult(TypedDict):
    metric: str
    value: float
    unit: str
    threshold: RequirementThreshold
    passed: bool
    evidence: dict[str, float | int]
    parameters: dict[str, float]


SUPPORTED_METRICS = set(get_args(MetricName))


@dataclass(frozen=True)
class MetricMeasurement:
    metric: str
    value: float
    unit: str
    evidence: dict[str, float | int]
    parameters: dict[str, float] = field(default_factory=dict)


def _real_vector(values: Sequence[Number], name: str) -> list[float]:
    if not values:
        raise ValueError(f"{name} must not be empty")
    result: list[float] = []
    for value in values:
        if isinstance(value, complex):
            if value.imag != 0.0:
                raise ValueError(f"{name} must be real-valued")
            number = float(value.real)
        else:
            number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{name} contains a non-finite value")
        result.append(number)
    return result


def _vectors(axis: Sequence[Number], values: Sequence[Number]) -> tuple[list[float], list[float]]:
    x = _real_vector(axis, "axis")
    y = _real_vector(values, "waveform")
    if len(x) != len(y):
        raise ValueError("axis and waveform must have the same number of points")
    return x, y


def _require_increasing(axis: Sequence[float]) -> None:
    if any(current <= previous for previous, current in zip(axis, axis[1:])):
        raise ValueError("axis must be strictly increasing for time-domain metrics")


def _point(index: int, axis: Sequence[float]) -> dict[str, float | int]:
    return {"index": index, "axis_value": axis[index]}


def _region(axis: Sequence[float]) -> dict[str, float | int]:
    return {
        "start_index": 0,
        "end_index": len(axis) - 1,
        "start_axis": axis[0],
        "end_axis": axis[-1],
    }


def _crossing(
    axis: Sequence[float],
    values: Sequence[float],
    level: float,
    rising: bool,
    start_index: int = 0,
) -> tuple[float, int, int]:
    if (rising and values[start_index] >= level) or (
        not rising and values[start_index] <= level
    ):
        return axis[start_index], start_index, start_index

    for index in range(max(1, start_index + 1), len(values)):
        before = values[index - 1]
        after = values[index]
        crossed = before <= level <= after if rising else before >= level >= after
        if not crossed:
            continue
        if after == before:
            return axis[index], index, index
        fraction = (level - before) / (after - before)
        crossing_axis = axis[index - 1] + fraction * (axis[index] - axis[index - 1])
        return crossing_axis, index - 1, index
    raise ValueError(f"waveform never crosses level {level}")


def measure_metric(
    axis: Sequence[Number],
    values: Sequence[Number],
    metric: str,
    *,
    signal_unit: str = "",
    axis_unit: str = "",
    initial_value: float | None = None,
    final_value: float | None = None,
    low_fraction: float = 0.1,
    high_fraction: float = 0.9,
    settling_tolerance: float = 0.02,
) -> MetricMeasurement:
    """Measure one property over the complete supplied waveform."""
    if metric not in SUPPORTED_METRICS:
        raise ValueError(f"Unknown waveform metric: {metric}")
    x, y = _vectors(axis, values)

    if metric == "minimum":
        index = min(range(len(y)), key=y.__getitem__)
        return MetricMeasurement(metric, y[index], signal_unit, _point(index, x))
    if metric == "maximum":
        index = max(range(len(y)), key=y.__getitem__)
        return MetricMeasurement(metric, y[index], signal_unit, _point(index, x))
    if metric == "mean":
        return MetricMeasurement(metric, math.fsum(y) / len(y), signal_unit, _region(x))
    if metric == "rms":
        value = math.sqrt(math.fsum(number * number for number in y) / len(y))
        return MetricMeasurement(metric, value, signal_unit, _region(x))
    if metric == "peak_to_peak":
        minimum_index = min(range(len(y)), key=y.__getitem__)
        maximum_index = max(range(len(y)), key=y.__getitem__)
        evidence = {
            "minimum_index": minimum_index,
            "minimum_axis": x[minimum_index],
            "maximum_index": maximum_index,
            "maximum_axis": x[maximum_index],
        }
        return MetricMeasurement(
            metric, y[maximum_index] - y[minimum_index], signal_unit, evidence
        )

    _require_increasing(x)
    initial = y[0] if initial_value is None else float(initial_value)
    final = y[-1] if final_value is None else float(final_value)
    amplitude = final - initial
    if not math.isfinite(initial) or not math.isfinite(final) or amplitude == 0.0:
        raise ValueError(f"{metric} requires distinct finite initial and final values")
    rising = amplitude > 0.0

    if metric == "rise_time":
        if not 0.0 <= low_fraction < high_fraction <= 1.0:
            raise ValueError("rise fractions must satisfy 0 <= low < high <= 1")
        low_level = initial + low_fraction * amplitude
        high_level = initial + high_fraction * amplitude
        low_axis, low_before, low_after = _crossing(x, y, low_level, rising)
        high_axis, high_before, high_after = _crossing(
            x, y, high_level, rising, low_before
        )
        evidence = {
            "low_axis": low_axis,
            "low_index_before": low_before,
            "low_index_after": low_after,
            "high_axis": high_axis,
            "high_index_before": high_before,
            "high_index_after": high_after,
        }
        parameters = {
            "initial_value": initial,
            "final_value": final,
            "low_fraction": low_fraction,
            "high_fraction": high_fraction,
        }
        return MetricMeasurement(
            metric, high_axis - low_axis, axis_unit, evidence, parameters
        )

    if metric == "overshoot":
        if rising:
            peak_index = max(range(len(y)), key=y.__getitem__)
            excursion = max(0.0, y[peak_index] - final)
        else:
            peak_index = min(range(len(y)), key=y.__getitem__)
            excursion = max(0.0, final - y[peak_index])
        evidence = _point(peak_index, x)
        evidence["waveform_value"] = y[peak_index]
        parameters = {"initial_value": initial, "final_value": final}
        return MetricMeasurement(
            metric, 100.0 * excursion / abs(amplitude), "%", evidence, parameters
        )

    if metric == "settling_time":
        if not 0.0 < settling_tolerance < 1.0:
            raise ValueError("settling_tolerance must be between 0 and 1")
        half_band = settling_tolerance * abs(amplitude)
        lower = final - half_band
        upper = final + half_band
        last_outside = next(
            (index for index in range(len(y) - 1, -1, -1) if not lower <= y[index] <= upper),
            None,
        )
        settling_index = 0 if last_outside is None else last_outside + 1
        if settling_index >= len(y):
            raise ValueError("waveform does not settle within the supplied data")
        evidence = _point(settling_index, x)
        evidence.update({"band_minimum": lower, "band_maximum": upper})
        parameters = {
            "initial_value": initial,
            "final_value": final,
            "settling_tolerance": settling_tolerance,
        }
        return MetricMeasurement(
            metric, x[settling_index] - x[0], axis_unit, evidence, parameters
        )


def evaluate_requirement(
    measurement: MetricMeasurement, operator: str, target: float
) -> RequirementResult:
    """Attach a threshold and deterministic pass/fail result to a measurement."""
    if not math.isfinite(target):
        raise ValueError("requirement target must be finite")
    comparisons = {
        "<": lambda actual: actual < target,
        "<=": lambda actual: actual <= target,
        ">": lambda actual: actual > target,
        ">=": lambda actual: actual >= target,
    }
    if operator not in comparisons:
        raise ValueError("operator must be one of <, <=, >, >=")
    return {
        "metric": measurement.metric,
        "value": measurement.value,
        "unit": measurement.unit,
        "threshold": {"operator": operator, "target": target, "unit": measurement.unit},
        "passed": comparisons[operator](measurement.value),
        "evidence": measurement.evidence,
        "parameters": measurement.parameters,
    }
