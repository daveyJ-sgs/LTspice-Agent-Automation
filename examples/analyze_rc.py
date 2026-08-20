#!/usr/bin/env python3
"""Run the RC example, export its waveform data, plot it, and check a result."""

from __future__ import annotations

import math
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ltspice_wrapper import DEFAULT_NETLIST, RUNS_DIR, parse_measurements, run_netlist
from raw_parser import export_csv, parse_raw


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = RUNS_DIR / f"analysis-{stamp}"
    run_netlist(DEFAULT_NETLIST, output_dir=output_dir)

    raw_path = output_dir / "rc_lowpass.raw"
    data = parse_raw(raw_path)
    csv_path = output_dir / "waveforms.csv"
    export_csv(data, csv_path)

    frequency = [float(complex(value).real) for value in data.values["frequency"]]
    vout = [complex(value) for value in data.values["V(out)"]]
    magnitude_db = [20 * math.log10(abs(value)) for value in vout]

    plot_path = output_dir / "vout_magnitude.png"
    plt.figure(figsize=(8, 4.5))
    plt.semilogx(frequency, magnitude_db)
    plt.grid(True, which="both", linestyle=":")
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("V(out) magnitude (dB)")
    plt.title("LTspice RC low-pass response")
    plt.tight_layout()
    plt.savefig(plot_path, dpi=150)
    plt.close()

    measurements = parse_measurements(output_dir / "rc_lowpass.log")
    actual = measurements["gain_at_1k"]
    expected = -35.9647
    tolerance = 0.05
    passed = abs(actual - expected) <= tolerance
    print(f"Raw variables: {', '.join(data.variables)}")
    print(f"Waveform CSV: {csv_path}")
    print(f"Plot: {plot_path}")
    print(f"PASS/FAIL: {'PASS' if passed else 'FAIL'}")
    print(f"  gain_at_1k = {actual:.4f} dB; expected {expected:.4f} +/- {tolerance:.2f} dB")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
