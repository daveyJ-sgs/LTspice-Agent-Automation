#!/usr/bin/env python3
"""Run an LTspice RC sweep and save measurements to CSV."""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

from ltspice_wrapper import PROJECT_DIR, RUNS_DIR, parse_measurements, run_netlist


RESISTANCES = [1_000, 2_200, 4_700, 10_000, 22_000, 47_000]


def make_netlist(resistance: int) -> str:
    return f"""* RC low-pass sweep, R={resistance}
V1 in 0 AC 1
R1 in out {resistance}
C1 out 0 1u
.ac dec 100 10 1Meg
.meas ac gain_at_1k FIND mag(V(out)) AT=1k
.end
"""


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sweep_dir = RUNS_DIR / f"sweep-{stamp}"
    input_dir = sweep_dir / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, float | int | str]] = []
    for resistance in RESISTANCES:
        netlist_path = input_dir / f"rc_{resistance}.cir"
        netlist_path.write_text(make_netlist(resistance))
        output_dir = sweep_dir / f"R_{resistance}"
        run_netlist(netlist_path, output_dir=output_dir)
        log_path = output_dir / netlist_path.with_suffix(".log").name
        measurements = parse_measurements(log_path)
        rows.append(
            {
                "resistance_ohms": resistance,
                "gain_at_1k_db": measurements["gain_at_1k"],
                "run_directory": str(output_dir),
            }
        )

    csv_path = sweep_dir / "results.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    database_path = sweep_dir / "results.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE sweep_results (
                resistance_ohms INTEGER NOT NULL,
                gain_at_1k_db REAL NOT NULL,
                run_directory TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO sweep_results VALUES (?, ?, ?)",
            [
                (row["resistance_ohms"], row["gain_at_1k_db"], row["run_directory"])
                for row in rows
            ],
        )

    history_path = RUNS_DIR / "history.sqlite3"
    with sqlite3.connect(history_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rc_sweep_history (
                sweep_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resistance_ohms INTEGER NOT NULL,
                gain_at_1k_db REAL NOT NULL,
                run_directory TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO rc_sweep_history VALUES (?, datetime('now'), ?, ?, ?)",
            [
                (sweep_dir.name, row["resistance_ohms"], row["gain_at_1k_db"], row["run_directory"])
                for row in rows
            ],
        )

    print(f"Sweep complete: {sweep_dir}")
    print(f"Results CSV: {csv_path}")
    print(f"Results SQLite: {database_path}")
    print(f"Cumulative history: {history_path}")
    for row in rows:
        print(f"  R={row['resistance_ohms']} ohms -> {row['gain_at_1k_db']:.4f} dB")


if __name__ == "__main__":
    main()
