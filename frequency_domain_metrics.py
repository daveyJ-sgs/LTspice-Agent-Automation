#!/usr/bin/env python3
"""Dependency-free spectral and AC measurements for LTspice vectors."""

from __future__ import annotations

import cmath
import math
from bisect import bisect_left
from collections.abc import Sequence

import waveform_metrics


Number = float | complex
SUPPORTED_METRICS = waveform_metrics.FREQUENCY_METRICS
MAX_SPECTRAL_BINS = 4096
MAX_SPECTRAL_WORK = 5_000_000
MAX_HARMONICS = 100


def _measurement(
    metric: str,
    value: float,
    unit: str,
    evidence: dict[str, float | int],
    window_parameters: dict[str, float],
    parameters: dict[str, float | int | str] | None = None,
) -> waveform_metrics.MetricMeasurement:
    return waveform_metrics.MetricMeasurement(
        metric,
        value,
        unit,
        evidence,
        {**window_parameters, **(parameters or {})},
    )


def _finite(value: float | None, name: str) -> float:
    if value is None:
        raise ValueError(f"{name} is required")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _frequency_unit(axis_unit: str) -> str:
    if axis_unit == "s":
        return "Hz"
    return f"1/{axis_unit}" if axis_unit else ""


def _max_gap(axis: Sequence[float]) -> float:
    if len(axis) < 2:
        raise ValueError("spectral metrics require at least two waveform points")
    return max(after - before for before, after in zip(axis, axis[1:]))


def _trapezoid_integral(axis: Sequence[float], values: Sequence[float]) -> float:
    return math.fsum(
        0.5 * (before + after) * (x_after - x_before)
        for x_before, x_after, before, after in zip(
            axis, axis[1:], values, values[1:]
        )
    )


def _spectral_samples(
    axis: Sequence[float],
    values: Sequence[float],
    *,
    hann: bool,
) -> tuple[list[float], float]:
    duration = axis[-1] - axis[0]
    if duration <= 0.0:
        raise ValueError("spectral metrics require a positive analysis duration")
    mean = _trapezoid_integral(axis, values) / duration

    def window(time: float) -> float:
        if not hann:
            return 1.0
        position = (time - axis[0]) / duration
        return 0.5 * (1.0 - math.cos(2.0 * math.pi * position))

    weighted: list[float] = []
    weights: list[float] = []
    for time, value in zip(axis, values):
        weight = window(time)
        weights.append(weight)
        weighted.append((value - mean) * weight)
    coherent_weight = _trapezoid_integral(axis, weights)
    if coherent_weight <= 0.0:
        raise ValueError("analysis window has insufficient points for spectral weighting")
    return weighted, coherent_weight


def _fourier_amplitude(
    axis: Sequence[float],
    weighted_values: Sequence[float],
    coherent_weight: float,
    frequency: float,
) -> float:
    angular_frequency = -2.0 * math.pi * frequency
    before_time = axis[0]
    before = weighted_values[0] * cmath.exp(1j * angular_frequency * before_time)
    coefficient = 0j
    for after_time, after_value in zip(axis[1:], weighted_values[1:]):
        after = after_value * cmath.exp(1j * angular_frequency * after_time)
        coefficient += 0.5 * (before + after) * (after_time - before_time)
        before_time = after_time
        before = after
    return 2.0 * abs(coefficient) / coherent_weight


def _check_spectral_work(point_count: int, frequency_count: int) -> None:
    if point_count * frequency_count > MAX_SPECTRAL_WORK:
        raise ValueError(
            f"spectral analysis is limited to {MAX_SPECTRAL_WORK} point-frequency operations"
        )


def _clip_end(
    axis: list[float],
    values: list[float],
    origins: list[tuple[int, int]],
    end: float,
) -> tuple[list[float], list[float], list[tuple[int, int]]]:
    position = bisect_left(axis, end)
    if position < len(axis) and axis[position] == end:
        return axis[: position + 1], values[: position + 1], origins[: position + 1]
    before = position - 1
    after = position
    fraction = (end - axis[before]) / (axis[after] - axis[before])
    value = values[before] + fraction * (values[after] - values[before])
    return (
        [*axis[:position], end],
        [*values[:position], value],
        [*origins[:position], (origins[before][0], origins[after][1])],
    )


def _complex_vector(values: Sequence[Number], name: str) -> list[complex]:
    if not values:
        raise ValueError(f"{name} must not be empty")
    result = []
    for value in values:
        number = complex(value)
        if not math.isfinite(number.real) or not math.isfinite(number.imag):
            raise ValueError(f"{name} contains a non-finite value")
        result.append(number)
    return result


def _unwrap_phase(values: Sequence[complex]) -> list[float]:
    phases = [math.degrees(cmath.phase(value)) for value in values]
    unwrapped = [phases[0]]
    for phase in phases[1:]:
        candidate = phase
        while candidate - unwrapped[-1] > 180.0:
            candidate -= 360.0
        while candidate - unwrapped[-1] < -180.0:
            candidate += 360.0
        if math.isclose(abs(candidate - unwrapped[-1]), 180.0, abs_tol=1e-12):
            raise ValueError("phase unwrap is ambiguous at an exact 180 degree step")
        unwrapped.append(candidate)
    return unwrapped


def _log_fraction(before: float, after: float, value: float) -> float:
    return math.log10(value / before) / math.log10(after / before)


def _window_ac(
    axis: Sequence[Number],
    values: Sequence[Number],
    secondary_values: Sequence[Number] | None,
    window_start: float | None,
    window_end: float | None,
    *,
    include_phase: bool,
) -> tuple[
    list[float],
    list[float],
    list[float],
    list[tuple[int, int]],
    dict[str, float],
]:
    frequency = waveform_metrics._real_vector(axis, "frequency axis")
    response = _complex_vector(values, "AC waveform")
    if len(frequency) != len(response):
        raise ValueError("frequency axis and AC waveform must have the same number of points")
    waveform_metrics._require_increasing(frequency)
    if frequency[0] <= 0.0:
        raise ValueError("AC frequency axis must be positive")
    if secondary_values is not None:
        reference = _complex_vector(secondary_values, "secondary AC waveform")
        if len(reference) != len(response):
            raise ValueError("AC waveforms must have the same number of points")
        if any(value == 0.0 for value in reference):
            raise ValueError("secondary AC waveform contains a zero reference value")
        response = [primary / secondary for primary, secondary in zip(response, reference)]
    magnitudes = [abs(value) for value in response]
    if any(value == 0.0 for value in magnitudes):
        raise ValueError("AC waveform contains a zero response magnitude")
    gain = [20.0 * math.log10(value) for value in magnitudes]
    phase = _unwrap_phase(response) if include_phase else [0.0] * len(response)

    start = frequency[0] if window_start is None else float(window_start)
    end = frequency[-1] if window_end is None else float(window_end)
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError("analysis window bounds must be finite")
    if start > end:
        raise ValueError("window_start must not exceed window_end")
    if start < frequency[0] or end > frequency[-1]:
        raise ValueError("analysis window must be within the captured frequency axis")

    def boundary(bound: float) -> tuple[float, float, float, tuple[int, int]]:
        position = bisect_left(frequency, bound)
        if position < len(frequency) and frequency[position] == bound:
            return bound, gain[position], phase[position], (position, position)
        before = position - 1
        after = position
        fraction = _log_fraction(frequency[before], frequency[after], bound)
        return (
            bound,
            gain[before] + fraction * (gain[after] - gain[before]),
            phase[before] + fraction * (phase[after] - phase[before]),
            (before, after),
        )

    start_point = boundary(start)
    end_point = boundary(end)
    interior = [index for index, value in enumerate(frequency) if start < value < end]
    selected_frequency = [start_point[0], *(frequency[index] for index in interior)]
    selected_gain = [start_point[1], *(gain[index] for index in interior)]
    selected_phase = [start_point[2], *(phase[index] for index in interior)]
    origins = [start_point[3], *((index, index) for index in interior)]
    if end > start:
        selected_frequency.append(end_point[0])
        selected_gain.append(end_point[1])
        selected_phase.append(end_point[2])
        origins.append(end_point[3])
    parameters = {}
    if window_start is not None:
        parameters["window_start"] = start
    if window_end is not None:
        parameters["window_end"] = end
    return selected_frequency, selected_gain, selected_phase, origins, parameters


def _interpolate_log(
    axis: Sequence[float], values: Sequence[float], frequency: float
) -> tuple[float, tuple[int, int]]:
    if frequency < axis[0] or frequency > axis[-1]:
        raise ValueError("requested frequency is outside the analysis window")
    position = bisect_left(axis, frequency)
    if position < len(axis) and axis[position] == frequency:
        return values[position], (position, position)
    before = position - 1
    after = position
    fraction = _log_fraction(axis[before], axis[after], frequency)
    value = values[before] + fraction * (values[after] - values[before])
    return value, (before, after)


def _crossings_log(
    axis: Sequence[float], values: Sequence[float], level: float, direction: str
) -> list[tuple[float, int, int]]:
    if direction not in ("rising", "falling"):
        raise ValueError("direction must be rising or falling")
    result = []
    for index in range(1, len(values)):
        before = values[index - 1]
        after = values[index]
        crossed = (
            before < level <= after
            or (index == 1 and before == level < after)
            if direction == "rising"
            else before > level >= after
            or (index == 1 and before == level > after)
        )
        if not crossed:
            continue
        fraction = (level - before) / (after - before)
        log_frequency = math.log10(axis[index - 1]) + fraction * math.log10(
            axis[index] / axis[index - 1]
        )
        result.append((10.0**log_frequency, index - 1, index))
    return result


def _single_crossing(
    events: list[tuple[float, int, int]], name: str
) -> tuple[float, int, int]:
    if not events:
        raise ValueError(f"{name} was not found in the analysis window")
    if len(events) > 1:
        raise ValueError(f"multiple {name} crossings; narrow the analysis window")
    return events[0]


def _crossing_evidence(
    event: tuple[float, int, int], origins: Sequence[tuple[int, int]]
) -> dict[str, float | int]:
    return {
        "frequency": event[0],
        "index_before": origins[event[1]][0],
        "index_after": origins[event[2]][1],
    }


def measure_metric(
    axis: Sequence[Number],
    values: Sequence[Number],
    metric: str,
    *,
    secondary_values: Sequence[Number] | None = None,
    signal_unit: str = "",
    axis_unit: str = "",
    window_start: float | None = None,
    window_end: float | None = None,
    threshold_value: float | None = None,
    edge: str = "rising",
    frequency_min: float | None = None,
    frequency_max: float | None = None,
    frequency_resolution: float | None = None,
    fundamental_frequency: float | None = None,
    maximum_harmonic: int = 5,
    frequency_value: float | None = None,
    reference_frequency: float | None = None,
    cutoff_drop_db: float = 3.01029995664,
    direction: str = "falling",
) -> waveform_metrics.MetricMeasurement:
    if metric not in SUPPORTED_METRICS:
        raise ValueError(f"Unknown frequency-domain metric: {metric}")

    if metric in {"frequency", "spectral_peak", "thd"}:
        x, y, _, origins, window_parameters = waveform_metrics._window(
            axis, values, None, window_start, window_end
        )
        waveform_metrics._require_increasing(x)
        frequency_unit = _frequency_unit(axis_unit)

        if metric == "frequency":
            threshold = _finite(threshold_value, "threshold_value")
            crossings = waveform_metrics._crossings(x, y, threshold, edge)
            if len(crossings) < 2:
                raise ValueError("frequency requires at least two matching crossings")
            first = crossings[0]
            last = crossings[-1]
            value = (len(crossings) - 1) / (last[0] - first[0])
            evidence = {
                "first_crossing_axis": first[0],
                "first_index_before": origins[first[1]][0],
                "first_index_after": origins[first[2]][1],
                "last_crossing_axis": last[0],
                "last_index_before": origins[last[1]][0],
                "last_index_after": origins[last[2]][1],
                "cycle_count": len(crossings) - 1,
            }
            return _measurement(
                metric,
                value,
                frequency_unit,
                evidence,
                window_parameters,
                {"threshold_value": threshold, "edge": edge},
            )

        if metric == "spectral_peak":
            minimum = _finite(frequency_min, "frequency_min")
            maximum = _finite(frequency_max, "frequency_max")
            if minimum <= 0.0 or maximum <= minimum:
                raise ValueError("spectral frequency band must satisfy 0 < minimum < maximum")
            duration = x[-1] - x[0]
            resolution = 1.0 / duration if frequency_resolution is None else float(
                frequency_resolution
            )
            if not math.isfinite(resolution) or resolution <= 0.0:
                raise ValueError("frequency_resolution must be positive and finite")
            count = math.floor((maximum - minimum) / resolution) + 1
            last_candidate = minimum + (count - 1) * resolution
            include_maximum = not math.isclose(
                last_candidate,
                maximum,
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
            if include_maximum:
                count += 1
            if count > MAX_SPECTRAL_BINS:
                raise ValueError(f"spectral analysis is limited to {MAX_SPECTRAL_BINS} bins")
            nyquist = 1.0 / (2.0 * _max_gap(x))
            if maximum > nyquist:
                raise ValueError("spectral frequency band exceeds the conservative Nyquist limit")
            _check_spectral_work(len(x), count)
            candidates = [
                minimum + index * resolution
                for index in range(count - int(include_maximum))
            ]
            if include_maximum:
                candidates.append(maximum)
            weighted, coherent_weight = _spectral_samples(x, y, hann=True)
            amplitudes = [
                _fourier_amplitude(x, weighted, coherent_weight, candidate)
                for candidate in candidates
            ]
            peak_index = max(range(len(amplitudes)), key=amplitudes.__getitem__)
            evidence = {
                **waveform_metrics._region(x, origins),
                "peak_frequency": candidates[peak_index],
                "frequency_resolution": resolution,
                "frequency_min": minimum,
                "frequency_max": maximum,
                "bin_count": len(candidates),
            }
            return _measurement(
                metric,
                amplitudes[peak_index],
                signal_unit,
                evidence,
                window_parameters,
                {
                    "frequency_min": minimum,
                    "frequency_max": maximum,
                    "frequency_resolution": resolution,
                    "spectral_window": "hann",
                },
            )

        fundamental = _finite(fundamental_frequency, "fundamental_frequency")
        if fundamental <= 0.0:
            raise ValueError("fundamental_frequency must be positive")
        if (
            not isinstance(maximum_harmonic, int)
            or isinstance(maximum_harmonic, bool)
            or maximum_harmonic < 2
            or maximum_harmonic > MAX_HARMONICS
        ):
            raise ValueError(
                f"maximum_harmonic must be an integer from 2 through {MAX_HARMONICS}"
            )
        cycle_count = math.floor((x[-1] - x[0]) * fundamental)
        if cycle_count < 1:
            raise ValueError("THD analysis window must contain at least one full cycle")
        effective_end = x[0] + cycle_count / fundamental
        x, y, origins = _clip_end(x, y, origins, effective_end)
        nyquist = 1.0 / (2.0 * _max_gap(x))
        if maximum_harmonic * fundamental > nyquist:
            raise ValueError("highest THD harmonic exceeds the conservative Nyquist limit")
        _check_spectral_work(len(x), maximum_harmonic)
        weighted, coherent_weight = _spectral_samples(x, y, hann=False)
        amplitudes = [
            _fourier_amplitude(
                x,
                weighted,
                coherent_weight,
                harmonic * fundamental,
            )
            for harmonic in range(1, maximum_harmonic + 1)
        ]
        if amplitudes[0] <= 1e-15:
            raise ValueError("THD fundamental amplitude is zero")
        harmonic_rss = math.sqrt(math.fsum(value * value for value in amplitudes[1:]))
        evidence = {
            **waveform_metrics._region(x, origins),
            "cycle_count": cycle_count,
            "fundamental_amplitude": amplitudes[0],
            "harmonic_rss": harmonic_rss,
            "maximum_harmonic": maximum_harmonic,
        }
        evidence.update(
            {
                f"harmonic_{index}_amplitude": amplitude
                for index, amplitude in enumerate(amplitudes, start=1)
            }
        )
        return _measurement(
            metric,
            100.0 * harmonic_rss / amplitudes[0],
            "%",
            evidence,
            window_parameters,
            {
                "fundamental_frequency": fundamental,
                "maximum_harmonic": maximum_harmonic,
            },
        )

    frequency, gain, phase, origins, window_parameters = _window_ac(
        axis,
        values,
        secondary_values,
        window_start,
        window_end,
        include_phase=metric
        in {"gain_crossover_frequency", "gain_margin", "phase_margin"},
    )
    frequency_axis_unit = axis_unit or "Hz"

    if metric == "ac_gain_db":
        selected_frequency = _finite(frequency_value, "frequency_value")
        value, local_origin = _interpolate_log(frequency, gain, selected_frequency)
        evidence = {
            "frequency": selected_frequency,
            **waveform_metrics._source_fields(
                "", (origins[local_origin[0]][0], origins[local_origin[1]][1])
            ),
        }
        return _measurement(
            metric,
            value,
            "dB",
            evidence,
            window_parameters,
            {"frequency_value": selected_frequency},
        )

    if metric in {"cutoff_frequency", "peaking_db"}:
        reference = _finite(reference_frequency, "reference_frequency")
        reference_gain, _ = _interpolate_log(frequency, gain, reference)
        if metric == "peaking_db":
            peak_index = max(range(len(gain)), key=gain.__getitem__)
            evidence = {
                "reference_frequency": reference,
                "reference_gain_db": reference_gain,
                "peak_frequency": frequency[peak_index],
                "peak_gain_db": gain[peak_index],
                **waveform_metrics._source_fields("peak_", origins[peak_index]),
            }
            return _measurement(
                metric,
                gain[peak_index] - reference_gain,
                "dB",
                evidence,
                window_parameters,
                {"reference_frequency": reference},
            )
        drop = float(cutoff_drop_db)
        if not math.isfinite(drop) or drop <= 0.0:
            raise ValueError("cutoff_drop_db must be positive and finite")
        cutoff_level = reference_gain - drop
        events = [
            event
            for event in _crossings_log(frequency, gain, cutoff_level, direction)
            if event[0] > reference
        ]
        event = _single_crossing(events, "cutoff")
        evidence = {
            **_crossing_evidence(event, origins),
            "reference_frequency": reference,
            "reference_gain_db": reference_gain,
            "cutoff_gain_db": cutoff_level,
            "crossing_count": len(events),
        }
        return _measurement(
            metric,
            event[0],
            frequency_axis_unit,
            evidence,
            window_parameters,
            {
                "reference_frequency": reference,
                "cutoff_drop_db": drop,
                "direction": direction,
            },
        )

    gain_crossings = _crossings_log(frequency, gain, 0.0, "falling")
    if metric in {"gain_crossover_frequency", "phase_margin"}:
        crossover = _single_crossing(gain_crossings, "gain crossover")
        crossover_phase, _ = _interpolate_log(frequency, phase, crossover[0])
        evidence = {
            **_crossing_evidence(crossover, origins),
            "gain_db": 0.0,
            "phase_degrees": crossover_phase,
            "crossing_count": len(gain_crossings),
        }
        if metric == "gain_crossover_frequency":
            return _measurement(
                metric,
                crossover[0],
                frequency_axis_unit,
                evidence,
                window_parameters,
            )
        return _measurement(
            metric,
            180.0 + crossover_phase,
            "deg",
            evidence,
            window_parameters,
        )

    minimum_phase = min(phase)
    maximum_phase = max(phase)
    first_level = math.ceil((minimum_phase + 180.0) / 360.0)
    last_level = math.floor((maximum_phase + 180.0) / 360.0)
    phase_crossings: list[tuple[tuple[float, int, int], float]] = []
    for multiple in range(first_level, last_level + 1):
        level = -180.0 + 360.0 * multiple
        phase_crossings.extend(
            (event, level)
            for event in _crossings_log(frequency, phase, level, "falling")
        )
    if not phase_crossings:
        raise ValueError("phase crossover was not found in the analysis window")
    if len(phase_crossings) > 1:
        raise ValueError("multiple phase crossover crossings; narrow the analysis window")
    crossover, level = phase_crossings[0]
    crossover_gain, _ = _interpolate_log(frequency, gain, crossover[0])
    evidence = {
        **_crossing_evidence(crossover, origins),
        "phase_degrees": level,
        "gain_db": crossover_gain,
        "crossing_count": len(phase_crossings),
    }
    return _measurement(
        metric,
        -crossover_gain,
        "dB",
        evidence,
        window_parameters,
    )
