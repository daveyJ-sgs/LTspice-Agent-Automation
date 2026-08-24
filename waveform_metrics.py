#!/usr/bin/env python3
"""Dependency-free, full-resolution waveform measurements and requirements."""

from __future__ import annotations

import math
from bisect import bisect_left
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
    "fall_time",
    "pulse_width",
    "duty_cycle",
    "slew_rate",
    "undershoot",
    "ripple",
    "monotonicity",
    "propagation_delay",
    "forbidden_region_samples",
]
ComparisonOperator = Literal["<", "<=", ">", ">="]
Edge = Literal["rising", "falling"]
Polarity = Literal["high", "low"]
Direction = Literal["rising", "falling"]


class WaveformRequirement(TypedDict):
    metric: MetricName
    operator: ComparisonOperator
    target: float
    initial_value: NotRequired[float]
    final_value: NotRequired[float]
    low_fraction: NotRequired[float]
    high_fraction: NotRequired[float]
    settling_tolerance: NotRequired[float]
    window_start: NotRequired[float]
    window_end: NotRequired[float]
    threshold_value: NotRequired[float]
    polarity: NotRequired[Polarity]
    primary_threshold: NotRequired[float]
    secondary_threshold: NotRequired[float]
    primary_edge: NotRequired[Edge]
    secondary_edge: NotRequired[Edge]
    forbidden_min: NotRequired[float]
    forbidden_max: NotRequired[float]
    secondary_forbidden_min: NotRequired[float]
    secondary_forbidden_max: NotRequired[float]
    direction: NotRequired[Direction]


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
    parameters: dict[str, float | str]


SUPPORTED_METRICS = set(get_args(MetricName))


@dataclass(frozen=True)
class MetricMeasurement:
    metric: str
    value: float
    unit: str
    evidence: dict[str, float | int]
    parameters: dict[str, float | str] = field(default_factory=dict)


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


def _source_fields(prefix: str, origin: tuple[int, int]) -> dict[str, int]:
    before, after = origin
    if before == after:
        return {f"{prefix}index": before}
    return {f"{prefix}index_before": before, f"{prefix}index_after": after}


def _point(
    index: int, axis: Sequence[float], origins: Sequence[tuple[int, int]]
) -> dict[str, float | int]:
    return {**_source_fields("", origins[index]), "axis_value": axis[index]}


def _region(
    axis: Sequence[float], origins: Sequence[tuple[int, int]]
) -> dict[str, float | int]:
    return {
        **_source_fields("start_", origins[0]),
        **_source_fields("end_", origins[-1]),
        "start_axis": axis[0],
        "end_axis": axis[-1],
    }


def _window(
    axis: Sequence[Number],
    values: Sequence[Number],
    secondary_values: Sequence[Number] | None,
    window_start: float | None,
    window_end: float | None,
) -> tuple[
    list[float],
    list[float],
    list[float] | None,
    list[tuple[int, int]],
    dict[str, float],
]:
    x, y = _vectors(axis, values)
    secondary = None
    if secondary_values is not None:
        secondary = _real_vector(secondary_values, "secondary waveform")
        if len(secondary) != len(x):
            raise ValueError("axis and secondary waveform must have the same number of points")

    parameters: dict[str, float] = {}
    if window_start is None and window_end is None:
        return x, y, secondary, [(index, index) for index in range(len(x))], parameters

    _require_increasing(x)
    start = x[0] if window_start is None else float(window_start)
    end = x[-1] if window_end is None else float(window_end)
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError("analysis window bounds must be finite")
    if start > end:
        raise ValueError("window_start must not exceed window_end")
    if start < x[0] or end > x[-1]:
        raise ValueError("analysis window must be within the captured axis")

    def boundary(bound: float) -> tuple[float, float, float | None, tuple[int, int]]:
        position = bisect_left(x, bound)
        if position < len(x) and x[position] == bound:
            secondary_value = None if secondary is None else secondary[position]
            return bound, y[position], secondary_value, (position, position)
        before = position - 1
        after = position
        fraction = (bound - x[before]) / (x[after] - x[before])
        primary_value = y[before] + fraction * (y[after] - y[before])
        secondary_value = None
        if secondary is not None:
            secondary_value = secondary[before] + fraction * (
                secondary[after] - secondary[before]
            )
        return bound, primary_value, secondary_value, (before, after)

    start_point = boundary(start)
    end_point = boundary(end)
    interior = [index for index, value in enumerate(x) if start < value < end]
    window_axis = [start_point[0], *(x[index] for index in interior)]
    window_values = [start_point[1], *(y[index] for index in interior)]
    window_origins = [start_point[3], *((index, index) for index in interior)]
    window_secondary = None
    if secondary is not None:
        window_secondary = [
            float(start_point[2]), *(secondary[index] for index in interior)
        ]
    if end > start:
        window_axis.append(end_point[0])
        window_values.append(end_point[1])
        window_origins.append(end_point[3])
        if window_secondary is not None:
            window_secondary.append(float(end_point[2]))
    if window_start is not None:
        parameters["window_start"] = start
    if window_end is not None:
        parameters["window_end"] = end
    return (
        window_axis,
        window_values,
        window_secondary,
        window_origins,
        parameters,
    )


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


def _crossings(
    axis: Sequence[float], values: Sequence[float], level: float, edge: str
) -> list[tuple[float, int, int]]:
    if edge not in ("rising", "falling"):
        raise ValueError("edge must be rising or falling")
    rising = edge == "rising"
    result: list[tuple[float, int, int]] = []
    for index in range(1, len(values)):
        before = values[index - 1]
        after = values[index]
        crossed = before < level <= after if rising else before > level >= after
        if not crossed:
            continue
        fraction = (level - before) / (after - before)
        crossing_axis = axis[index - 1] + fraction * (axis[index] - axis[index - 1])
        result.append((crossing_axis, index - 1, index))
    return result


def _finite_parameter(value: float | None, name: str) -> float:
    if value is None:
        raise ValueError(f"{name} is required")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _ratio_unit(signal_unit: str, axis_unit: str) -> str:
    if signal_unit and axis_unit:
        return f"{signal_unit}/{axis_unit}"
    if signal_unit:
        return signal_unit
    if axis_unit:
        return f"1/{axis_unit}"
    return ""


def measure_metric(
    axis: Sequence[Number],
    values: Sequence[Number],
    metric: str,
    *,
    secondary_values: Sequence[Number] | None = None,
    signal_unit: str = "",
    axis_unit: str = "",
    initial_value: float | None = None,
    final_value: float | None = None,
    low_fraction: float = 0.1,
    high_fraction: float = 0.9,
    settling_tolerance: float = 0.02,
    window_start: float | None = None,
    window_end: float | None = None,
    threshold_value: float | None = None,
    polarity: str = "high",
    primary_threshold: float | None = None,
    secondary_threshold: float | None = None,
    primary_edge: str | None = None,
    secondary_edge: str | None = None,
    forbidden_min: float | None = None,
    forbidden_max: float | None = None,
    secondary_forbidden_min: float | None = None,
    secondary_forbidden_max: float | None = None,
    direction: str | None = None,
) -> MetricMeasurement:
    """Measure one property over the complete supplied waveform."""
    if metric not in SUPPORTED_METRICS:
        raise ValueError(f"Unknown waveform metric: {metric}")
    x, y, secondary, origins, window_parameters = _window(
        axis, values, secondary_values, window_start, window_end
    )

    def measurement(
        value: float,
        unit: str,
        evidence: dict[str, float | int],
        parameters: dict[str, float | str] | None = None,
    ) -> MetricMeasurement:
        return MetricMeasurement(
            metric,
            value,
            unit,
            evidence,
            {**window_parameters, **(parameters or {})},
        )

    if metric == "minimum":
        index = min(range(len(y)), key=y.__getitem__)
        return measurement(y[index], signal_unit, _point(index, x, origins))
    if metric == "maximum":
        index = max(range(len(y)), key=y.__getitem__)
        return measurement(y[index], signal_unit, _point(index, x, origins))
    if metric == "mean":
        return measurement(math.fsum(y) / len(y), signal_unit, _region(x, origins))
    if metric == "rms":
        value = math.sqrt(math.fsum(number * number for number in y) / len(y))
        return measurement(value, signal_unit, _region(x, origins))
    if metric in ("peak_to_peak", "ripple"):
        minimum_index = min(range(len(y)), key=y.__getitem__)
        maximum_index = max(range(len(y)), key=y.__getitem__)
        evidence = {
            **_source_fields("minimum_", origins[minimum_index]),
            "minimum_axis": x[minimum_index],
            **_source_fields("maximum_", origins[maximum_index]),
            "maximum_axis": x[maximum_index],
        }
        return measurement(y[maximum_index] - y[minimum_index], signal_unit, evidence)

    if metric == "slew_rate":
        _require_increasing(x)
        if len(x) < 2:
            raise ValueError("slew_rate requires at least two waveform points")
        slopes = [
            abs((after - before) / (x[index] - x[index - 1]))
            for index, (before, after) in enumerate(zip(y, y[1:]), start=1)
        ]
        segment = max(range(len(slopes)), key=slopes.__getitem__)
        evidence = {
            **_source_fields("start_", origins[segment]),
            **_source_fields("end_", origins[segment + 1]),
            "start_axis": x[segment],
            "end_axis": x[segment + 1],
        }
        return measurement(slopes[segment], _ratio_unit(signal_unit, axis_unit), evidence)

    if metric in ("pulse_width", "duty_cycle"):
        _require_increasing(x)
        if len(x) < 2:
            raise ValueError(f"{metric} requires at least two waveform points")
        threshold = _finite_parameter(threshold_value, "threshold_value")
        if polarity not in ("high", "low"):
            raise ValueError("polarity must be high or low")
        entry_edge = "rising" if polarity == "high" else "falling"
        exit_edge = "falling" if polarity == "high" else "rising"
        parameters: dict[str, float | str] = {
            "threshold_value": threshold,
            "polarity": polarity,
        }
        if metric == "pulse_width":
            entries = _crossings(x, y, threshold, entry_edge)
            exits = _crossings(x, y, threshold, exit_edge)
            pair = next(
                (
                    (entry, exit_event)
                    for entry in entries
                    for exit_event in exits
                    if exit_event[0] > entry[0]
                ),
                None,
            )
            if pair is None:
                raise ValueError("waveform has no complete pulse in the analysis window")
            entry, exit_event = pair
            evidence = {
                "entry_axis": entry[0],
                "entry_index_before": origins[entry[1]][0],
                "entry_index_after": origins[entry[2]][1],
                "exit_axis": exit_event[0],
                "exit_index_before": origins[exit_event[1]][0],
                "exit_index_after": origins[exit_event[2]][1],
            }
            return measurement(exit_event[0] - entry[0], axis_unit, evidence, parameters)

        active_duration = 0.0
        for index in range(1, len(y)):
            before = y[index - 1]
            after = y[index]
            before_active = before >= threshold if polarity == "high" else before <= threshold
            after_active = after >= threshold if polarity == "high" else after <= threshold
            duration = x[index] - x[index - 1]
            if before_active and after_active:
                active_duration += duration
            elif before_active != after_active:
                fraction = (threshold - before) / (after - before)
                active_duration += duration * (fraction if before_active else 1.0 - fraction)
        total_duration = x[-1] - x[0]
        evidence = {
            **_region(x, origins),
            "active_duration": active_duration,
            "total_duration": total_duration,
        }
        return measurement(100.0 * active_duration / total_duration, "%", evidence, parameters)

    if metric == "propagation_delay":
        _require_increasing(x)
        if secondary is None:
            raise ValueError("propagation_delay requires a secondary waveform")
        trigger_threshold = _finite_parameter(primary_threshold, "primary_threshold")
        response_threshold = _finite_parameter(secondary_threshold, "secondary_threshold")
        if primary_edge is None or secondary_edge is None:
            raise ValueError("primary_edge and secondary_edge are required")
        triggers = _crossings(x, y, trigger_threshold, primary_edge)
        if not triggers:
            raise ValueError("primary waveform never crosses its threshold")
        trigger = triggers[0]
        responses = _crossings(x, secondary, response_threshold, secondary_edge)
        response = next((event for event in responses if event[0] >= trigger[0]), None)
        if response is None:
            raise ValueError("secondary waveform has no matching crossing after the primary")
        evidence = {
            "primary_axis": trigger[0],
            "primary_index_before": origins[trigger[1]][0],
            "primary_index_after": origins[trigger[2]][1],
            "secondary_axis": response[0],
            "secondary_index_before": origins[response[1]][0],
            "secondary_index_after": origins[response[2]][1],
        }
        parameters = {
            "primary_threshold": trigger_threshold,
            "secondary_threshold": response_threshold,
            "primary_edge": primary_edge,
            "secondary_edge": secondary_edge,
        }
        return measurement(response[0] - trigger[0], axis_unit, evidence, parameters)

    if metric == "forbidden_region_samples":
        lower = _finite_parameter(forbidden_min, "forbidden_min")
        upper = _finite_parameter(forbidden_max, "forbidden_max")
        if lower > upper:
            raise ValueError("forbidden_min must not exceed forbidden_max")
        has_secondary_bounds = (
            secondary_forbidden_min is not None or secondary_forbidden_max is not None
        )
        if has_secondary_bounds and secondary is None:
            raise ValueError("secondary forbidden bounds require a secondary waveform")
        secondary_lower = secondary_upper = None
        if has_secondary_bounds:
            secondary_lower = _finite_parameter(
                secondary_forbidden_min, "secondary_forbidden_min"
            )
            secondary_upper = _finite_parameter(
                secondary_forbidden_max, "secondary_forbidden_max"
            )
            if secondary_lower > secondary_upper:
                raise ValueError(
                    "secondary_forbidden_min must not exceed secondary_forbidden_max"
                )
        violations = []
        for index, value in enumerate(y):
            if origins[index][0] != origins[index][1]:
                continue
            if not lower <= value <= upper:
                continue
            if secondary_lower is not None and not (
                secondary_lower <= secondary[index] <= secondary_upper
            ):
                continue
            violations.append(index)
        evidence: dict[str, float | int] = {"violation_count": len(violations)}
        if violations:
            evidence.update(
                {
                    "first_index": origins[violations[0]][0],
                    "first_axis": x[violations[0]],
                    "last_index": origins[violations[-1]][0],
                    "last_axis": x[violations[-1]],
                }
            )
        parameters = {"forbidden_min": lower, "forbidden_max": upper}
        if secondary_lower is not None:
            parameters.update(
                {
                    "secondary_forbidden_min": secondary_lower,
                    "secondary_forbidden_max": secondary_upper,
                }
            )
        return measurement(float(len(violations)), "points", evidence, parameters)

    _require_increasing(x)
    initial = y[0] if initial_value is None else float(initial_value)
    final = y[-1] if final_value is None else float(final_value)
    amplitude = final - initial
    if not math.isfinite(initial) or not math.isfinite(final):
        raise ValueError(f"{metric} requires finite initial and final values")

    if metric == "monotonicity":
        if direction is None:
            if amplitude == 0.0:
                raise ValueError(
                    "monotonicity requires direction when initial and final values are equal"
                )
            direction = "rising" if amplitude > 0.0 else "falling"
        if direction not in ("rising", "falling"):
            raise ValueError("direction must be rising or falling")
        rising = direction == "rising"
        reversals = [
            max(0.0, before - after) if rising else max(0.0, after - before)
            for before, after in zip(y, y[1:])
        ]
        if not reversals:
            raise ValueError("monotonicity requires at least two waveform points")
        segment = max(range(len(reversals)), key=reversals.__getitem__)
        evidence = {
            **_source_fields("start_", origins[segment]),
            **_source_fields("end_", origins[segment + 1]),
            "start_axis": x[segment],
            "end_axis": x[segment + 1],
        }
        parameters = {
            "initial_value": initial,
            "final_value": final,
            "direction": direction,
        }
        return measurement(reversals[segment], signal_unit, evidence, parameters)

    if amplitude == 0.0:
        raise ValueError(f"{metric} requires distinct finite initial and final values")
    rising = amplitude > 0.0

    if metric in ("rise_time", "fall_time"):
        if not 0.0 <= low_fraction < high_fraction <= 1.0:
            raise ValueError("transition fractions must satisfy 0 <= low < high <= 1")
        if metric == "rise_time" and not rising:
            raise ValueError("rise_time requires final_value greater than initial_value")
        if metric == "fall_time" and rising:
            raise ValueError("fall_time requires final_value less than initial_value")
        low_level = initial + low_fraction * amplitude
        high_level = initial + high_fraction * amplitude
        low_axis, low_before, low_after = _crossing(x, y, low_level, rising)
        high_axis, high_before, high_after = _crossing(
            x, y, high_level, rising, low_before
        )
        evidence = {
            "low_axis": low_axis,
            "low_index_before": origins[low_before][0],
            "low_index_after": origins[low_after][1],
            "high_axis": high_axis,
            "high_index_before": origins[high_before][0],
            "high_index_after": origins[high_after][1],
        }
        parameters = {
            "initial_value": initial,
            "final_value": final,
            "low_fraction": low_fraction,
            "high_fraction": high_fraction,
        }
        return measurement(high_axis - low_axis, axis_unit, evidence, parameters)

    if metric == "overshoot":
        if rising:
            peak_index = max(range(len(y)), key=y.__getitem__)
            excursion = max(0.0, y[peak_index] - final)
        else:
            peak_index = min(range(len(y)), key=y.__getitem__)
            excursion = max(0.0, final - y[peak_index])
        evidence = _point(peak_index, x, origins)
        evidence["waveform_value"] = y[peak_index]
        parameters = {"initial_value": initial, "final_value": final}
        return measurement(100.0 * excursion / abs(amplitude), "%", evidence, parameters)

    if metric == "undershoot":
        if rising:
            peak_index = min(range(len(y)), key=y.__getitem__)
            excursion = max(0.0, initial - y[peak_index])
        else:
            peak_index = max(range(len(y)), key=y.__getitem__)
            excursion = max(0.0, y[peak_index] - initial)
        evidence = _point(peak_index, x, origins)
        evidence["waveform_value"] = y[peak_index]
        parameters = {"initial_value": initial, "final_value": final}
        return measurement(100.0 * excursion / abs(amplitude), "%", evidence, parameters)

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
        evidence = _point(settling_index, x, origins)
        evidence.update({"band_minimum": lower, "band_maximum": upper})
        parameters = {
            "initial_value": initial,
            "final_value": final,
            "settling_tolerance": settling_tolerance,
        }
        return measurement(x[settling_index] - x[0], axis_unit, evidence, parameters)


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
