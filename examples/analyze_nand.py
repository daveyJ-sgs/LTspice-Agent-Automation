#!/usr/bin/env python3
"""Run a transistor-level CMOS NAND truth-table and timing experiment."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from checks import assert_between, assert_close, real_values
from ltspice_wrapper import RUNS_DIR, parse_measurements, run_netlist
from raw_parser import export_csv, parse_raw


PROJECT_DIR = Path(__file__).resolve().parent.parent
NETLIST = PROJECT_DIR / "examples" / "cmos_nand.cir"
VDD = 3.3
THRESHOLD = VDD / 2


def _sample_at(time: list[float], values: list[float], target: float) -> float:
    index = min(range(len(time)), key=lambda item: abs(time[item] - target))
    return values[index]


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = RUNS_DIR / f"nand-{stamp}"
    run_netlist(NETLIST, output_dir=output_dir, ascii_raw=True)

    data = parse_raw(output_dir / "cmos_nand.raw")
    export_csv(data, output_dir / "waveforms.csv")
    time = real_values(data.values["time"])
    a = real_values(data.values["V(a)"])
    b = real_values(data.values["V(b)"])
    out = real_values(data.values["V(out)"])
    reference = real_values(data.values["V(out_ref)"])

    # These are stable portions of each input combination in the 80 us test.
    sample_times = [5e-6, 15e-6, 25e-6, 45e-6, 55e-6, 65e-6]
    observed: list[tuple[int, int, int]] = []
    for target in sample_times:
        a_bit = int(_sample_at(time, a, target) > THRESHOLD)
        b_bit = int(_sample_at(time, b, target) > THRESHOLD)
        output_voltage = _sample_at(time, out, target)
        out_bit = int(output_voltage > THRESHOLD)
        expected = int(not (a_bit and b_bit))
        assert_close(f"NAND({a_bit},{b_bit}) at {target:g}s", out_bit, expected, 0)
        if expected:
            assert_between("logic-high output", output_voltage, 2.5, VDD + 0.1)
        else:
            assert_between("logic-low output", output_voltage, -0.1, 0.5)
        observed.append((a_bit, b_bit, out_bit))

    measurements = parse_measurements(output_dir / "cmos_nand.log")
    assert_between("fall propagation delay", abs(measurements["tpd_fall"]), 0, 5e-6)
    assert_between("rise propagation delay", abs(measurements["tpd_rise"]), 0, 5e-6)

    time_us = [value * 1e6 for value in time]
    plot_specs = [
        ("nand_input_a.png", "Input A", a, "tab:blue"),
        ("nand_input_b.png", "Input B", b, "tab:orange"),
        ("nand_output.png", "CMOS NAND output", out, "tab:green"),
        ("nand_behavioral_reference.png", "Behavioral reference", reference, "tab:red"),
    ]
    plot_paths: list[Path] = []
    for filename, label, values, color in plot_specs:
        plot_path = output_dir / filename
        plt.figure(figsize=(9, 3.5))
        draw = plt.step if label in {"Input A", "Input B"} else plt.plot
        draw(time_us, values, label=label, color=color, linewidth=2)
        plt.axhline(THRESHOLD, color="0.65", linestyle=":", linewidth=1)
        plt.grid(True, linestyle=":")
        plt.xlabel("Time (µs)")
        plt.ylabel("Voltage (V)")
        plt.title(f"3.3 V CMOS NAND: {label}")
        plt.legend()
        plt.tight_layout()
        plt.savefig(plot_path, dpi=150)
        plt.close()
        plot_paths.append(plot_path)

    print(f"Raw points: {data.points}")
    print(f"Waveform CSV: {output_dir / 'waveforms.csv'}")
    print("Plots:")
    for plot_path in plot_paths:
        print(f"  {plot_path}")
    print(f"Truth-table samples: {observed}")
    print("PASS/FAIL: PASS")
    print(f"  tpd_fall = {measurements['tpd_fall']:.6g} s")
    print(f"  tpd_rise = {measurements['tpd_rise']:.6g} s")


if __name__ == "__main__":
    main()
