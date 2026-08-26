#!/usr/bin/env python3
"""Bounded real-LTspice smoke used by the opt-in Windows workflow."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import frequency_domain_metrics
from ltspice_wrapper import parse_measurements, run_netlist
from raw_parser import parse_raw

PROJECT_DIR = Path(__file__).resolve().parent.parent


def main() -> None:
    evidence_dir_value = os.environ.get("REAL_LTSPICE_EVIDENCE_DIR")
    if not evidence_dir_value:
        raise RuntimeError("REAL_LTSPICE_EVIDENCE_DIR is required")
    evidence_dir = Path(evidence_dir_value)
    output_dir = run_netlist(
        PROJECT_DIR / "examples" / "rc_lowpass.cir",
        output_dir=evidence_dir,
        timeout_seconds=30,
    )
    raw = parse_raw(output_dir / "rc_lowpass.raw")
    measurements = parse_measurements(output_dir / "rc_lowpass.log")
    cutoff = frequency_domain_metrics.measure_metric(
        raw.values["frequency"],
        raw.values["V(out)"],
        "cutoff_frequency",
        secondary_values=raw.values["V(in)"],
        reference_frequency=10.0,
    )

    gain_at_1k = measurements["gain_at_1k"]
    if not -36.2 <= gain_at_1k <= -35.7:
        raise AssertionError(f"unexpected 1 kHz gain: {gain_at_1k}")
    if not 21.0 <= cutoff.value <= 22.0:
        raise AssertionError(f"unexpected cutoff frequency: {cutoff.value}")
    if raw.step_count != 1 or raw.points < 500:
        raise AssertionError("real RAW waveform shape is incomplete")

    manifest = json.loads(
        (output_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    summary = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "simulator": manifest["simulator"],
        "execution_source": manifest["execution_source"],
        "raw_points": raw.points,
        "gain_at_1k_db": gain_at_1k,
        "cutoff_frequency_hz": cutoff.value,
    }
    (output_dir / "smoke_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
