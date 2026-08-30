#!/usr/bin/env python3
"""Dependency-free, full-resolution waveform measurements and requirements."""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
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
    "frequency",
    "spectral_peak",
    "thd",
    "ac_gain_db",
    "cutoff_frequency",
    "peaking_db",
    "gain_crossover_frequency",
    "gain_margin",
    "phase_margin",
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
    edge: NotRequired[Edge]
    frequency_min: NotRequired[float]
    frequency_max: NotRequired[float]
    frequency_resolution: NotRequired[float]
    fundamental_frequency: NotRequired[float]
    maximum_harmonic: NotRequired[int]
    frequency_value: NotRequired[float]
    reference_frequency: NotRequired[float]
    cutoff_drop_db: NotRequired[float]


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
    parameters: dict[str, float | int | str]


FREQUENCY_METRICS = {
    "frequency",
    "spectral_peak",
    "thd",
    "ac_gain_db",
    "cutoff_frequency",
    "peaking_db",
    "gain_crossover_frequency",
    "gain_margin",
    "phase_margin",
}
SUPPORTED_METRICS = set(get_args(MetricName)) - FREQUENCY_METRICS


@dataclass(frozen=True)
class MetricMeasurement:
    metric: str
    value: float
    unit: str
    evidence: dict[str, float | int]
    parameters: dict[str, float | int | str] = field(default_factory=dict)


@dataclass(frozen=True)
class _MetricSpec:
    handler: Callable[["_WaveformMetricRequest"], MetricMeasurement]
    parameters: frozenset[str]


@dataclass(frozen=True)
class _WaveformMetricRequest:
    metric: str
    axis: list[float]
    values: list[float]
    secondary_values: list[float] | None
    origins: list[tuple[int, int]]
    window_parameters: dict[str, float]
    signal_unit: str
    axis_unit: str
    initial_value: float | None
    final_value: float | None
    low_fraction: float
    high_fraction: float
    settling_tolerance: float
    threshold_value: float | None
    polarity: str
    primary_threshold: float | None
    secondary_threshold: float | None
    primary_edge: str | None
    secondary_edge: str | None
    forbidden_min: float | None
    forbidden_max: float | None
    secondary_forbidden_min: float | None
    secondary_forbidden_max: float | None
    direction: str | None


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


def _vectors(
    axis: Sequence[Number], values: Sequence[Number]
) -> tuple[list[float], list[float]]:
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
            raise ValueError(
                "axis and secondary waveform must have the same number of points"
            )

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
        assert start_point[2] is not None
        window_secondary = [
            float(start_point[2]),
            *(secondary[index] for index in interior),
        ]
    if end > start:
        window_axis.append(end_point[0])
        window_values.append(end_point[1])
        window_origins.append(end_point[3])
        if window_secondary is not None:
            assert end_point[2] is not None
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


def _measurement(
    request: _WaveformMetricRequest,
    value: float,
    unit: str,
    evidence: dict[str, float | int],
    parameters: Mapping[str, float | int | str] | None = None,
) -> MetricMeasurement:
    return MetricMeasurement(
        request.metric,
        value,
        unit,
        evidence,
        {**request.window_parameters, **(parameters or {})},
    )


def _measure_minimum(request: _WaveformMetricRequest) -> MetricMeasurement:
    index = min(range(len(request.values)), key=request.values.__getitem__)
    return _measurement(
        request,
        request.values[index],
        request.signal_unit,
        _point(index, request.axis, request.origins),
    )


def _measure_maximum(request: _WaveformMetricRequest) -> MetricMeasurement:
    index = max(range(len(request.values)), key=request.values.__getitem__)
    return _measurement(
        request,
        request.values[index],
        request.signal_unit,
        _point(index, request.axis, request.origins),
    )


def _measure_mean(request: _WaveformMetricRequest) -> MetricMeasurement:
    return _measurement(
        request,
        math.fsum(request.values) / len(request.values),
        request.signal_unit,
        _region(request.axis, request.origins),
    )


def _measure_rms(request: _WaveformMetricRequest) -> MetricMeasurement:
    value = math.sqrt(
        math.fsum(number * number for number in request.values) / len(request.values)
    )
    return _measurement(
        request,
        value,
        request.signal_unit,
        _region(request.axis, request.origins),
    )


def _measure_span(request: _WaveformMetricRequest) -> MetricMeasurement:
    minimum_index = min(range(len(request.values)), key=request.values.__getitem__)
    maximum_index = max(range(len(request.values)), key=request.values.__getitem__)
    evidence = {
        **_source_fields("minimum_", request.origins[minimum_index]),
        "minimum_axis": request.axis[minimum_index],
        **_source_fields("maximum_", request.origins[maximum_index]),
        "maximum_axis": request.axis[maximum_index],
    }
    return _measurement(
        request,
        request.values[maximum_index] - request.values[minimum_index],
        request.signal_unit,
        evidence,
    )


def _measure_slew_rate(request: _WaveformMetricRequest) -> MetricMeasurement:
    _require_increasing(request.axis)
    if len(request.axis) < 2:
        raise ValueError("slew_rate requires at least two waveform points")
    slopes = [
        abs((after - before) / (request.axis[index] - request.axis[index - 1]))
        for index, (before, after) in enumerate(
            zip(request.values, request.values[1:]), start=1
        )
    ]
    segment = max(range(len(slopes)), key=slopes.__getitem__)
    evidence = {
        **_source_fields("start_", request.origins[segment]),
        **_source_fields("end_", request.origins[segment + 1]),
        "start_axis": request.axis[segment],
        "end_axis": request.axis[segment + 1],
    }
    return _measurement(
        request,
        slopes[segment],
        _ratio_unit(request.signal_unit, request.axis_unit),
        evidence,
    )


def _measure_pulse(request: _WaveformMetricRequest) -> MetricMeasurement:
    _require_increasing(request.axis)
    if len(request.axis) < 2:
        raise ValueError(f"{request.metric} requires at least two waveform points")
    threshold = _finite_parameter(request.threshold_value, "threshold_value")
    if request.polarity not in ("high", "low"):
        raise ValueError("polarity must be high or low")
    entry_edge = "rising" if request.polarity == "high" else "falling"
    exit_edge = "falling" if request.polarity == "high" else "rising"
    parameters: dict[str, float | str] = {
        "threshold_value": threshold,
        "polarity": request.polarity,
    }
    if request.metric == "pulse_width":
        entries = _crossings(request.axis, request.values, threshold, entry_edge)
        exits = _crossings(request.axis, request.values, threshold, exit_edge)
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
            "entry_index_before": request.origins[entry[1]][0],
            "entry_index_after": request.origins[entry[2]][1],
            "exit_axis": exit_event[0],
            "exit_index_before": request.origins[exit_event[1]][0],
            "exit_index_after": request.origins[exit_event[2]][1],
        }
        return _measurement(
            request,
            exit_event[0] - entry[0],
            request.axis_unit,
            evidence,
            parameters,
        )

    active_duration = 0.0
    for index in range(1, len(request.values)):
        before = request.values[index - 1]
        after = request.values[index]
        before_active = (
            before >= threshold if request.polarity == "high" else before <= threshold
        )
        after_active = (
            after >= threshold if request.polarity == "high" else after <= threshold
        )
        duration = request.axis[index] - request.axis[index - 1]
        if before_active and after_active:
            active_duration += duration
        elif before_active != after_active:
            fraction = (threshold - before) / (after - before)
            active_duration += duration * (
                fraction if before_active else 1.0 - fraction
            )
    total_duration = request.axis[-1] - request.axis[0]
    evidence = {
        **_region(request.axis, request.origins),
        "active_duration": active_duration,
        "total_duration": total_duration,
    }
    return _measurement(
        request,
        100.0 * active_duration / total_duration,
        "%",
        evidence,
        parameters,
    )


def _measure_propagation_delay(
    request: _WaveformMetricRequest,
) -> MetricMeasurement:
    _require_increasing(request.axis)
    if request.secondary_values is None:
        raise ValueError("propagation_delay requires a secondary waveform")
    trigger_threshold = _finite_parameter(
        request.primary_threshold, "primary_threshold"
    )
    response_threshold = _finite_parameter(
        request.secondary_threshold, "secondary_threshold"
    )
    if request.primary_edge is None or request.secondary_edge is None:
        raise ValueError("primary_edge and secondary_edge are required")
    primary_edge = request.primary_edge
    secondary_edge = request.secondary_edge
    triggers = _crossings(request.axis, request.values, trigger_threshold, primary_edge)
    if not triggers:
        raise ValueError("primary waveform never crosses its threshold")
    trigger = triggers[0]
    responses = _crossings(
        request.axis,
        request.secondary_values,
        response_threshold,
        secondary_edge,
    )
    response = next((event for event in responses if event[0] >= trigger[0]), None)
    if response is None:
        raise ValueError(
            "secondary waveform has no matching crossing after the primary"
        )
    evidence = {
        "primary_axis": trigger[0],
        "primary_index_before": request.origins[trigger[1]][0],
        "primary_index_after": request.origins[trigger[2]][1],
        "secondary_axis": response[0],
        "secondary_index_before": request.origins[response[1]][0],
        "secondary_index_after": request.origins[response[2]][1],
    }
    parameters: dict[str, float | int | str] = {
        "primary_threshold": trigger_threshold,
        "secondary_threshold": response_threshold,
        "primary_edge": primary_edge,
        "secondary_edge": secondary_edge,
    }
    return _measurement(
        request,
        response[0] - trigger[0],
        request.axis_unit,
        evidence,
        parameters,
    )


def _measure_forbidden_region(
    request: _WaveformMetricRequest,
) -> MetricMeasurement:
    lower = _finite_parameter(request.forbidden_min, "forbidden_min")
    upper = _finite_parameter(request.forbidden_max, "forbidden_max")
    if lower > upper:
        raise ValueError("forbidden_min must not exceed forbidden_max")
    has_secondary_bounds = (
        request.secondary_forbidden_min is not None
        or request.secondary_forbidden_max is not None
    )
    if has_secondary_bounds and request.secondary_values is None:
        raise ValueError("secondary forbidden bounds require a secondary waveform")
    secondary_lower = secondary_upper = None
    if has_secondary_bounds:
        secondary_lower = _finite_parameter(
            request.secondary_forbidden_min, "secondary_forbidden_min"
        )
        secondary_upper = _finite_parameter(
            request.secondary_forbidden_max, "secondary_forbidden_max"
        )
        if secondary_lower > secondary_upper:
            raise ValueError(
                "secondary_forbidden_min must not exceed secondary_forbidden_max"
            )
    violations = []
    secondary_values = request.secondary_values
    secondary_upper_value = secondary_upper
    if secondary_lower is not None:
        assert secondary_upper_value is not None
        assert secondary_values is not None
    for index, value in enumerate(request.values):
        if request.origins[index][0] != request.origins[index][1]:
            continue
        if not lower <= value <= upper:
            continue
        if secondary_lower is not None:
            assert secondary_values is not None
            assert secondary_upper_value is not None
            if not (
                secondary_lower <= secondary_values[index] <= secondary_upper_value
            ):
                continue
        violations.append(index)
    evidence: dict[str, float | int] = {"violation_count": len(violations)}
    if violations:
        evidence.update(
            {
                "first_index": request.origins[violations[0]][0],
                "first_axis": request.axis[violations[0]],
                "last_index": request.origins[violations[-1]][0],
                "last_axis": request.axis[violations[-1]],
            }
        )
    parameters: dict[str, float | int | str] = {
        "forbidden_min": lower,
        "forbidden_max": upper,
    }
    if secondary_lower is not None:
        assert secondary_upper_value is not None
        parameters.update(
            {
                "secondary_forbidden_min": secondary_lower,
                "secondary_forbidden_max": secondary_upper_value,
            }
        )
    return _measurement(
        request,
        float(len(violations)),
        "points",
        evidence,
        parameters,
    )


def _transition_state(
    request: _WaveformMetricRequest,
) -> tuple[float, float, float]:
    _require_increasing(request.axis)
    initial = (
        request.values[0]
        if request.initial_value is None
        else float(request.initial_value)
    )
    final = (
        request.values[-1]
        if request.final_value is None
        else float(request.final_value)
    )
    if not math.isfinite(initial) or not math.isfinite(final):
        raise ValueError(f"{request.metric} requires finite initial and final values")
    return initial, final, final - initial


def _measure_monotonicity(request: _WaveformMetricRequest) -> MetricMeasurement:
    initial, final, amplitude = _transition_state(request)
    direction = request.direction
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
        for before, after in zip(request.values, request.values[1:])
    ]
    if not reversals:
        raise ValueError("monotonicity requires at least two waveform points")
    segment = max(range(len(reversals)), key=reversals.__getitem__)
    evidence = {
        **_source_fields("start_", request.origins[segment]),
        **_source_fields("end_", request.origins[segment + 1]),
        "start_axis": request.axis[segment],
        "end_axis": request.axis[segment + 1],
    }
    parameters: dict[str, float | int | str] = {
        "initial_value": initial,
        "final_value": final,
        "direction": direction,
    }
    return _measurement(
        request,
        reversals[segment],
        request.signal_unit,
        evidence,
        parameters,
    )


def _transition_direction(
    request: _WaveformMetricRequest,
) -> tuple[float, float, float, bool]:
    initial, final, amplitude = _transition_state(request)
    if amplitude == 0.0:
        raise ValueError(
            f"{request.metric} requires distinct finite initial and final values"
        )
    return initial, final, amplitude, amplitude > 0.0


def _measure_transition_time(
    request: _WaveformMetricRequest,
) -> MetricMeasurement:
    initial, final, amplitude, rising = _transition_direction(request)
    if not 0.0 <= request.low_fraction < request.high_fraction <= 1.0:
        raise ValueError("transition fractions must satisfy 0 <= low < high <= 1")
    if request.metric == "rise_time" and not rising:
        raise ValueError("rise_time requires final_value greater than initial_value")
    if request.metric == "fall_time" and rising:
        raise ValueError("fall_time requires final_value less than initial_value")
    low_level = initial + request.low_fraction * amplitude
    high_level = initial + request.high_fraction * amplitude
    low_axis, low_before, low_after = _crossing(
        request.axis, request.values, low_level, rising
    )
    high_axis, high_before, high_after = _crossing(
        request.axis,
        request.values,
        high_level,
        rising,
        low_before,
    )
    evidence = {
        "low_axis": low_axis,
        "low_index_before": request.origins[low_before][0],
        "low_index_after": request.origins[low_after][1],
        "high_axis": high_axis,
        "high_index_before": request.origins[high_before][0],
        "high_index_after": request.origins[high_after][1],
    }
    parameters = {
        "initial_value": initial,
        "final_value": final,
        "low_fraction": request.low_fraction,
        "high_fraction": request.high_fraction,
    }
    return _measurement(
        request,
        high_axis - low_axis,
        request.axis_unit,
        evidence,
        parameters,
    )


def _measure_overshoot(request: _WaveformMetricRequest) -> MetricMeasurement:
    initial, final, amplitude, rising = _transition_direction(request)
    if rising:
        peak_index = max(range(len(request.values)), key=request.values.__getitem__)
        excursion = max(0.0, request.values[peak_index] - final)
    else:
        peak_index = min(range(len(request.values)), key=request.values.__getitem__)
        excursion = max(0.0, final - request.values[peak_index])
    evidence = _point(peak_index, request.axis, request.origins)
    evidence["waveform_value"] = request.values[peak_index]
    parameters = {"initial_value": initial, "final_value": final}
    return _measurement(
        request,
        100.0 * excursion / abs(amplitude),
        "%",
        evidence,
        parameters,
    )


def _measure_undershoot(request: _WaveformMetricRequest) -> MetricMeasurement:
    initial, final, amplitude, rising = _transition_direction(request)
    if rising:
        peak_index = min(range(len(request.values)), key=request.values.__getitem__)
        excursion = max(0.0, initial - request.values[peak_index])
    else:
        peak_index = max(range(len(request.values)), key=request.values.__getitem__)
        excursion = max(0.0, request.values[peak_index] - initial)
    evidence = _point(peak_index, request.axis, request.origins)
    evidence["waveform_value"] = request.values[peak_index]
    parameters = {"initial_value": initial, "final_value": final}
    return _measurement(
        request,
        100.0 * excursion / abs(amplitude),
        "%",
        evidence,
        parameters,
    )


def _measure_settling_time(
    request: _WaveformMetricRequest,
) -> MetricMeasurement:
    initial, final, amplitude, _ = _transition_direction(request)
    if not 0.0 < request.settling_tolerance < 1.0:
        raise ValueError("settling_tolerance must be between 0 and 1")
    half_band = request.settling_tolerance * abs(amplitude)
    lower = final - half_band
    upper = final + half_band
    last_outside = next(
        (
            index
            for index in range(len(request.values) - 1, -1, -1)
            if not lower <= request.values[index] <= upper
        ),
        None,
    )
    settling_index = 0 if last_outside is None else last_outside + 1
    if settling_index >= len(request.values):
        raise ValueError("waveform does not settle within the supplied data")
    evidence = _point(settling_index, request.axis, request.origins)
    evidence.update({"band_minimum": lower, "band_maximum": upper})
    parameters = {
        "initial_value": initial,
        "final_value": final,
        "settling_tolerance": request.settling_tolerance,
    }
    return _measurement(
        request,
        request.axis[settling_index] - request.axis[0],
        request.axis_unit,
        evidence,
        parameters,
    )


_METRIC_REGISTRY = {
    "minimum": _MetricSpec(_measure_minimum, frozenset()),
    "maximum": _MetricSpec(_measure_maximum, frozenset()),
    "mean": _MetricSpec(_measure_mean, frozenset()),
    "rms": _MetricSpec(_measure_rms, frozenset()),
    "peak_to_peak": _MetricSpec(_measure_span, frozenset()),
    "ripple": _MetricSpec(_measure_span, frozenset()),
    "slew_rate": _MetricSpec(_measure_slew_rate, frozenset()),
    "pulse_width": _MetricSpec(
        _measure_pulse, frozenset({"threshold_value", "polarity"})
    ),
    "duty_cycle": _MetricSpec(
        _measure_pulse, frozenset({"threshold_value", "polarity"})
    ),
    "propagation_delay": _MetricSpec(
        _measure_propagation_delay,
        frozenset(
            {
                "secondary_values",
                "primary_threshold",
                "secondary_threshold",
                "primary_edge",
                "secondary_edge",
            }
        ),
    ),
    "forbidden_region_samples": _MetricSpec(
        _measure_forbidden_region,
        frozenset(
            {
                "secondary_values",
                "forbidden_min",
                "forbidden_max",
                "secondary_forbidden_min",
                "secondary_forbidden_max",
            }
        ),
    ),
    "monotonicity": _MetricSpec(
        _measure_monotonicity,
        frozenset({"initial_value", "final_value", "direction"}),
    ),
    "rise_time": _MetricSpec(
        _measure_transition_time,
        frozenset({"initial_value", "final_value", "low_fraction", "high_fraction"}),
    ),
    "fall_time": _MetricSpec(
        _measure_transition_time,
        frozenset({"initial_value", "final_value", "low_fraction", "high_fraction"}),
    ),
    "overshoot": _MetricSpec(
        _measure_overshoot, frozenset({"initial_value", "final_value"})
    ),
    "undershoot": _MetricSpec(
        _measure_undershoot, frozenset({"initial_value", "final_value"})
    ),
    "settling_time": _MetricSpec(
        _measure_settling_time,
        frozenset({"initial_value", "final_value", "settling_tolerance"}),
    ),
}


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
    spec = _METRIC_REGISTRY.get(metric)
    if spec is None:
        raise ValueError(f"Unknown waveform metric: {metric}")
    x, y, secondary, origins, window_parameters = _window(
        axis, values, secondary_values, window_start, window_end
    )
    request = _WaveformMetricRequest(
        metric=metric,
        axis=x,
        values=y,
        secondary_values=secondary,
        origins=origins,
        window_parameters=window_parameters,
        signal_unit=signal_unit,
        axis_unit=axis_unit,
        initial_value=initial_value,
        final_value=final_value,
        low_fraction=low_fraction,
        high_fraction=high_fraction,
        settling_tolerance=settling_tolerance,
        threshold_value=threshold_value,
        polarity=polarity,
        primary_threshold=primary_threshold,
        secondary_threshold=secondary_threshold,
        primary_edge=primary_edge,
        secondary_edge=secondary_edge,
        forbidden_min=forbidden_min,
        forbidden_max=forbidden_max,
        secondary_forbidden_min=secondary_forbidden_min,
        secondary_forbidden_max=secondary_forbidden_max,
        direction=direction,
    )
    return spec.handler(request)


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
