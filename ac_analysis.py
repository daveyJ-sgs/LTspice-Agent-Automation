#!/usr/bin/env python3
"""Gain and phase post-processing for complex AC vectors in a parsed .raw file."""

from __future__ import annotations

import cmath
import math
from collections.abc import Sequence

from raw_parser import RawData, step_slices


def _resolve_variable(data: RawData, name: str) -> str:
    """Match a node/branch name case-insensitively, as LTspice may recase it."""
    folded = name.casefold()
    for candidate in data.variables:
        if candidate.casefold() == folded:
            return candidate
    available = ", ".join(data.variables)
    raise ValueError(f"{name} is not in the RAW file; available vectors: {available}")


def unwrap_degrees(phases: Sequence[float]) -> list[float]:
    """Remove 360-degree wraps, carrying the last finite sample across gaps.

    frequency_domain_metrics._unwrap_phase deliberately rejects an exact
    180-degree step because an ambiguous branch would silently move a stability
    margin. This variant is for plotting and export instead, so it tolerates
    both that step and the non-finite samples that degenerate points produce.
    """
    unwrapped: list[float] = []
    reference: float | None = None
    for phase in phases:
        if not math.isfinite(phase):
            unwrapped.append(phase)
            continue
        if reference is None:
            candidate = phase
        else:
            candidate = phase - 360.0 * round((phase - reference) / 360.0)
        unwrapped.append(candidate)
        reference = candidate
    return unwrapped


def gain_phase(
    data: RawData,
    numerator: str,
    denominator: str,
    unwrap_phase: bool = False,
) -> list[dict[str, list[float]]]:
    """Compute gain (dB) and phase (deg) of numerator/denominator per step.

    Returns one dict per ``.step`` block, each containing:
        - "frequency": list[float]  (independent axis for that step)
        - "gain_db":   list[float]
        - "phase_deg": list[float]

    A zero denominator yields nan for both gain and phase at that point, and a
    zero numerator yields -inf dB, so one degenerate point cannot kill a sweep.

    Raises ValueError if either variable is missing from the RAW file, or if
    the file has no Complex flag set (not an AC-analysis dataset).
    """
    if "complex" not in data.flags.lower():
        raise ValueError(
            "gain_phase requires an AC-analysis RAW file with the Complex flag; "
            f"found flags: {data.flags.strip() or '(none)'}"
        )
    numerator_key = _resolve_variable(data, numerator)
    denominator_key = _resolve_variable(data, denominator)
    axis_key = data.variables[0]

    results: list[dict[str, list[float]]] = []
    for block in step_slices(data):
        axis = data.values[axis_key][block]
        numerators = data.values[numerator_key][block]
        denominators = data.values[denominator_key][block]
        frequency = [
            float(value.real if isinstance(value, complex) else value) for value in axis
        ]
        gain_db: list[float] = []
        phase_deg: list[float] = []
        for top, bottom in zip(numerators, denominators):
            if bottom == 0:
                gain_db.append(math.nan)
                phase_deg.append(math.nan)
                continue
            ratio = complex(top) / complex(bottom)
            magnitude = abs(ratio)
            gain_db.append(-math.inf if magnitude == 0.0 else 20.0 * math.log10(magnitude))
            phase_deg.append(math.degrees(cmath.phase(ratio)))
        results.append(
            {
                "frequency": frequency,
                "gain_db": gain_db,
                "phase_deg": unwrap_degrees(phase_deg) if unwrap_phase else phase_deg,
            }
        )
    return results
