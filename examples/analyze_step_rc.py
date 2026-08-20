#!/usr/bin/env python3
"""Run one native LTspice .step analysis and export one row per step."""

from __future__ import annotations

import csv
from datetime import datetime

from ltspice_wrapper import RUNS_DIR, parse_step_values, parse_stepped_measurements, run_netlist
from raw_parser import parse_raw

from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
NETLIST = PROJECT_DIR / "examples" / "step_rc.cir"


def main() -> None:
    run_root = RUNS_DIR / f"native-step-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_netlist(NETLIST, output_dir=run_root)
    raw_path = run_root / "step_rc.raw"
    log_path = run_root / "step_rc.log"
    raw = parse_raw(raw_path)
    resistances = parse_step_values(log_path, "rval")
    gains = parse_stepped_measurements(log_path, "gain_at_1k")
    if raw.step_count != len(resistances) or len(gains) != len(resistances):
        raise RuntimeError("Native step metadata and measurement rows do not agree")

    csv_path = run_root / "results.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["step", "resistance_ohms", "gain_at_1k_db"])
        writer.writeheader()
        writer.writerows(
            {"step": index, "resistance_ohms": resistance, "gain_at_1k_db": gain}
            for index, (resistance, gain) in enumerate(zip(resistances, gains), start=1)
        )

    print(f"Native .step analysis: {run_root}")
    print(f"Raw points: {raw.points}; detected steps: {raw.step_count}; points/step: {raw.points_per_step}")
    print(f"Results CSV: {csv_path}")
    for resistance, gain in zip(resistances, gains):
        print(f"  R={resistance:g} ohms -> {gain:.4f} dB")


if __name__ == "__main__":
    main()
