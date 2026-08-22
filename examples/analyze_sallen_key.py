#!/usr/bin/env python3
"""Run a Sallen-Key 2nd-order low-pass filter, check it, and plot both domains.

Unlike the single-pole RC low-pass, this is a second-order active filter
(ideal op-amp modeled as a fixed-gain E-source, K=2.5) tuned for Q=2. That
produces resonant peaking in the frequency response and pronounced
overshoot/ringing in the step response, exercising checks that a first-order
circuit cannot.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from checks import assert_between, real_values
from ltspice_wrapper import RUNS_DIR, parse_measurements, run_netlist
from raw_parser import export_csv, parse_raw

PROJECT_DIR = Path(__file__).resolve().parent.parent
AC_NETLIST = PROJECT_DIR / "examples" / "sallen_key_lowpass.cir"
STEP_NETLIST = PROJECT_DIR / "examples" / "sallen_key_step.cir"

# K=2.5 with equal R=15.9k/C=10n legs gives Q = 1/(3-K) = 2 and
# fc = 1/(2*pi*R*C) ~= 1000 Hz; see LEARNINGS.md for the derivation.
EXPECTED_GAIN_DC_DB = 20 * math.log10(2.5)
EXPECTED_FC_HZ = 1000.0
EXPECTED_Q = 2.0


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # --- frequency response ---
    ac_dir = RUNS_DIR / f"sallen-key-ac-{stamp}"
    # Binary .raw, not ASCII: the "Values:" ASCII format used elsewhere in
    # this project only carries real vectors, and AC analysis is complex.
    run_netlist(AC_NETLIST, output_dir=ac_dir)
    ac_data = parse_raw(ac_dir / "sallen_key_lowpass.raw")
    export_csv(ac_data, ac_dir / "waveforms.csv")

    frequency = real_values(ac_data.values["frequency"])
    vout_ac = [complex(value) for value in ac_data.values["V(out)"]]
    magnitude_db = [20 * math.log10(abs(value)) for value in vout_ac]
    peak_index = max(range(len(magnitude_db)), key=lambda i: magnitude_db[i])
    peak_freq, peak_db = frequency[peak_index], magnitude_db[peak_index]

    ac_measurements = parse_measurements(ac_dir / "sallen_key_lowpass.log")
    assert_between("DC gain", ac_measurements["gain_dc"], EXPECTED_GAIN_DC_DB - 0.1, EXPECTED_GAIN_DC_DB + 0.1)
    assert_between("peak frequency", peak_freq, 700, 1300)
    peaking_db = ac_measurements["peak_gain"] - ac_measurements["gain_dc"]
    assert_between("resonant peaking above passband", peaking_db, 5.0, 7.5)

    bode_path = ac_dir / "sallen_key_bode.png"
    plt.figure(figsize=(8, 4.5))
    plt.semilogx(frequency, magnitude_db, color="tab:blue", linewidth=2)
    plt.axhline(ac_measurements["gain_dc"], color="0.65", linestyle=":", linewidth=1, label="passband gain")
    plt.scatter([peak_freq], [peak_db], color="tab:red", zorder=3, label=f"peak {peak_db:.2f} dB @ {peak_freq:.0f} Hz")
    plt.grid(True, which="both", linestyle=":")
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("V(out) magnitude (dB)")
    plt.title("Sallen-Key low-pass: resonant peaking (Q=2)")
    plt.legend()
    plt.tight_layout()
    plt.savefig(bode_path, dpi=150)
    plt.close()

    # --- step response ---
    step_dir = RUNS_DIR / f"sallen-key-step-{stamp}"
    run_netlist(STEP_NETLIST, output_dir=step_dir, ascii_raw=True)
    step_data = parse_raw(step_dir / "sallen_key_step.raw")
    export_csv(step_data, step_dir / "waveforms.csv")

    time_ms = [value * 1e3 for value in real_values(step_data.values["time"])]
    vout_step = real_values(step_data.values["V(out)"])

    step_measurements = parse_measurements(step_dir / "sallen_key_step.log")
    assert_between("settled output", step_measurements["vout_final"], 2.4, 2.6)
    assert_between("overshoot", step_measurements["overshoot_pct"], 35, 55)

    step_path = step_dir / "sallen_key_step.png"
    plt.figure(figsize=(8, 4.5))
    plt.plot(time_ms, vout_step, color="tab:green", linewidth=2)
    plt.axhline(step_measurements["vout_final"], color="0.65", linestyle=":", linewidth=1, label="settled value")
    plt.grid(True, linestyle=":")
    plt.xlabel("Time (ms)")
    plt.ylabel("V(out) (V)")
    plt.title(f"Sallen-Key step response: {step_measurements['overshoot_pct']:.1f}% overshoot")
    plt.legend()
    plt.tight_layout()
    plt.savefig(step_path, dpi=150)
    plt.close()

    print(f"AC run: {ac_dir}")
    print(f"Step run: {step_dir}")
    print(f"Bode plot: {bode_path}")
    print(f"Step plot: {step_path}")
    print("PASS/FAIL: PASS")
    print(f"  gain_dc = {ac_measurements['gain_dc']:.4f} dB (expected {EXPECTED_GAIN_DC_DB:.4f})")
    print(f"  peak = {peak_db:.4f} dB @ {peak_freq:.1f} Hz (+{peaking_db:.2f} dB above passband)")
    print(f"  vout_final = {step_measurements['vout_final']:.4f} V")
    print(f"  overshoot = {step_measurements['overshoot_pct']:.2f}% (Q={EXPECTED_Q:.1f} predicts ~44%)")


if __name__ == "__main__":
    main()
