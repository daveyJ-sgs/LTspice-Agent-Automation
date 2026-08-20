#!/usr/bin/env python3
"""Run a deterministic RC Monte Carlo analysis and save yield statistics."""

from __future__ import annotations

import csv
import random
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ltspice_wrapper import RUNS_DIR, parse_measurements, run_netlist


SAMPLES = 24
SEED = 20260819
R_NOMINAL = 10_000.0
C_NOMINAL = 1e-6
COMPONENT_SIGMA = 0.05
GAIN_MIN_DB = -38.0
GAIN_MAX_DB = -34.0


def make_netlist(resistance: float, capacitance: float) -> str:
    return f"""* RC Monte Carlo sample
V1 in 0 AC 1
R1 in out {resistance:.12g}
C1 out 0 {capacitance:.12g}
.ac dec 100 10 1Meg
.meas ac gain_at_1k FIND mag(V(out)) AT=1k
.end
"""


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root = RUNS_DIR / f"monte-carlo-{stamp}"
    input_dir = run_root / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)

    generator = random.Random(SEED)
    rows: list[dict[str, float | int | str | bool]] = []
    for sample in range(1, SAMPLES + 1):
        resistance = generator.gauss(R_NOMINAL, R_NOMINAL * COMPONENT_SIGMA)
        capacitance = generator.gauss(C_NOMINAL, C_NOMINAL * COMPONENT_SIGMA)
        resistance = max(resistance, R_NOMINAL * 0.01)
        capacitance = max(capacitance, C_NOMINAL * 0.01)
        netlist_path = input_dir / f"sample_{sample:03d}.cir"
        netlist_path.write_text(make_netlist(resistance, capacitance))
        output_dir = run_root / f"sample_{sample:03d}"
        run_netlist(netlist_path, output_dir=output_dir)
        log_path = output_dir / netlist_path.with_suffix(".log").name
        gain = parse_measurements(log_path)["gain_at_1k"]
        passed = GAIN_MIN_DB <= gain <= GAIN_MAX_DB
        rows.append(
            {
                "sample": sample,
                "resistance_ohms": resistance,
                "capacitance_f": capacitance,
                "gain_at_1k_db": gain,
                "passed": passed,
                "run_directory": str(output_dir),
            }
        )

    csv_path = run_root / "results.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    database_path = run_root / "results.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE monte_carlo_results (
                sample INTEGER NOT NULL,
                resistance_ohms REAL NOT NULL,
                capacitance_f REAL NOT NULL,
                gain_at_1k_db REAL NOT NULL,
                passed INTEGER NOT NULL,
                run_directory TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO monte_carlo_results VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    row["sample"],
                    row["resistance_ohms"],
                    row["capacitance_f"],
                    row["gain_at_1k_db"],
                    int(row["passed"]),
                    row["run_directory"],
                )
                for row in rows
            ],
        )

    gains = [float(row["gain_at_1k_db"]) for row in rows]
    passed_count = sum(bool(row["passed"]) for row in rows)
    plot_path = run_root / "gain_distribution.png"
    plt.figure(figsize=(8, 4.5))
    plt.hist(gains, bins=8, edgecolor="black")
    plt.axvline(GAIN_MIN_DB, color="red", linestyle="--", label="lower limit")
    plt.axvline(GAIN_MAX_DB, color="red", linestyle="--", label="upper limit")
    plt.xlabel("Gain at 1 kHz (dB)")
    plt.ylabel("Samples")
    plt.title("RC Monte Carlo gain distribution")
    plt.legend()
    plt.tight_layout()
    plt.savefig(plot_path, dpi=150)
    plt.close()

    history_path = RUNS_DIR / "history.sqlite3"
    with sqlite3.connect(history_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monte_carlo_history (
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                samples INTEGER NOT NULL,
                passed INTEGER NOT NULL,
                mean_gain_db REAL NOT NULL,
                stdev_gain_db REAL NOT NULL,
                run_directory TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO monte_carlo_history VALUES (?, datetime('now'), ?, ?, ?, ?, ?)",
            (
                run_root.name,
                SAMPLES,
                passed_count,
                statistics.mean(gains),
                statistics.stdev(gains),
                str(run_root),
            ),
        )

    print(f"Monte Carlo complete: {run_root}")
    print(f"Results CSV: {csv_path}")
    print(f"Results SQLite: {database_path}")
    print(f"Distribution plot: {plot_path}")
    print(f"Yield: {passed_count}/{SAMPLES} ({100 * passed_count / SAMPLES:.1f}%)")
    print(f"Gain mean/stdev: {statistics.mean(gains):.4f}/{statistics.stdev(gains):.4f} dB")


if __name__ == "__main__":
    main()
