#!/usr/bin/env python3
"""Run a transient RC simulation and check/export its waveform."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from checks import assert_between, floor, peak
from ltspice_wrapper import RUNS_DIR, parse_measurements, run_netlist
from raw_parser import export_csv, parse_raw


PROJECT_DIR = Path(__file__).resolve().parent.parent
NETLIST = PROJECT_DIR / "examples" / "transient_rc.cir"


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = RUNS_DIR / f"transient-{stamp}"
    run_netlist(NETLIST, output_dir=output_dir, ascii_raw=True)

    raw_path = output_dir / "transient_rc.raw"
    data = parse_raw(raw_path)
    csv_path = output_dir / "waveforms.csv"
    export_csv(data, csv_path)

    time = [float(complex(value).real) for value in data.values["time"]]
    vout = data.values["V(out)"]
    vout_real = [float(complex(value).real) for value in vout]
    plot_path = output_dir / "vout_transient.png"
    plt.figure(figsize=(8, 4.5))
    plt.plot(time, vout_real)
    plt.grid(True, linestyle=":")
    plt.xlabel("Time (s)")
    plt.ylabel("V(out) (V)")
    plt.title("LTspice RC transient response")
    plt.tight_layout()
    plt.savefig(plot_path, dpi=150)
    plt.close()

    measurements = parse_measurements(output_dir / "transient_rc.log")
    measured_peak = measurements["vout_max"]
    measured_floor = measurements["vout_min"]
    assert_between("vout_max measurement", measured_peak, 4.95, 5.05)
    assert_between("vout_min measurement", measured_floor, -0.05, 0.05)
    assert_between("V(out) waveform peak", peak(vout), 4.95, 5.05)
    assert_between("V(out) waveform floor", floor(vout), -0.05, 0.05)

    print(f"Raw points: {data.points}")
    print(f"Waveform CSV: {csv_path}")
    print(f"Plot: {plot_path}")
    print(f"PASS/FAIL: PASS")
    print(f"  measured peak = {measured_peak:.6g} V")
    print(f"  measured floor = {measured_floor:.6g} V")


if __name__ == "__main__":
    main()
