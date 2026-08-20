#!/usr/bin/env python3
"""Find an RC resistance that hits a target gain at 1 kHz."""

from __future__ import annotations

import csv
import html
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ltspice_wrapper import RUNS_DIR, parse_measurements, run_netlist


TARGET_DB = -30.0
LOWER_OHMS = 1_000.0
UPPER_OHMS = 100_000.0
ITERATIONS = 12


def make_netlist(resistance: float) -> str:
    return f"""* RC target search, R={resistance:.12g}
V1 in 0 AC 1
R1 in out {resistance:.12g}
C1 out 0 1u
.ac dec 100 10 1Meg
.meas ac gain_at_1k FIND mag(V(out)) AT=1k
.end
"""


def choose_best(rows: list[dict[str, float | int | str]], target_db: float) -> dict[str, float | int | str]:
    return min(rows, key=lambda row: abs(float(row["gain_at_1k_db"]) - target_db))


def write_report(run_root: Path, rows: list[dict[str, float | int | str]], best: dict[str, float | int | str]) -> Path:
    report_path = run_root / "report.html"
    table_rows = "\n".join(
        "<tr>"
        + "".join(f"<td>{html.escape(str(row[field]))}</td>" for field in ("trial", "resistance_ohms", "gain_at_1k_db", "error_db"))
        + "</tr>"
        for row in rows
    )
    report_path.write_text(
        f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>LTspice RC design search</title>
<style>body{{font:16px system-ui;max-width:900px;margin:2rem auto}}table{{border-collapse:collapse}}th,td{{border:1px solid #ccc;padding:.4rem .7rem;text-align:right}}th{{background:#eee}}img{{max-width:100%}}</style>
<h1>LTspice RC design search</h1>
<p>Target gain at 1 kHz: <strong>{TARGET_DB:.4f} dB</strong></p>
<p>Best resistance: <strong>{float(best['resistance_ohms']):.6g} ohms</strong>; gain: <strong>{float(best['gain_at_1k_db']):.6f} dB</strong>; error: <strong>{float(best['error_db']):.6f} dB</strong></p>
<img src="search_progress.png" alt="Search progress plot">
<table><thead><tr><th>Trial</th><th>Resistance (ohms)</th><th>Gain (dB)</th><th>Error (dB)</th></tr></thead><tbody>{table_rows}</tbody></table>
"""
    )
    return report_path


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root = RUNS_DIR / f"design-search-{stamp}"
    input_dir = run_root / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, float | int | str]] = []
    low = LOWER_OHMS
    high = UPPER_OHMS
    for trial in range(1, ITERATIONS + 1):
        resistance = math.sqrt(low * high)
        netlist_path = input_dir / f"trial_{trial:02d}.cir"
        netlist_path.write_text(make_netlist(resistance))
        output_dir = run_root / f"trial_{trial:02d}"
        run_netlist(netlist_path, output_dir=output_dir)
        log_path = output_dir / netlist_path.with_suffix(".log").name
        gain = parse_measurements(log_path)["gain_at_1k"]
        rows.append(
            {
                "trial": trial,
                "resistance_ohms": resistance,
                "gain_at_1k_db": gain,
                "error_db": gain - TARGET_DB,
                "run_directory": str(output_dir),
            }
        )
        if gain > TARGET_DB:
            low = resistance
        else:
            high = resistance

    best = choose_best(rows, TARGET_DB)
    csv_path = run_root / "trials.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    plot_path = run_root / "search_progress.png"
    plt.figure(figsize=(8, 4.5))
    plt.semilogx([row["resistance_ohms"] for row in rows], [row["gain_at_1k_db"] for row in rows], "o-")
    plt.axhline(TARGET_DB, color="red", linestyle="--", label="target")
    plt.xlabel("Resistance (ohms)")
    plt.ylabel("Gain at 1 kHz (dB)")
    plt.title("LTspice RC target search")
    plt.grid(True, which="both", linestyle=":")
    plt.legend()
    plt.tight_layout()
    plt.savefig(plot_path, dpi=150)
    plt.close()

    database_path = run_root / "results.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE trials (trial INTEGER, resistance_ohms REAL, gain_at_1k_db REAL, error_db REAL, run_directory TEXT)")
        connection.executemany("INSERT INTO trials VALUES (?, ?, ?, ?, ?)", [tuple(row.values()) for row in rows])
    report_path = write_report(run_root, rows, best)

    print(f"Design search complete: {run_root}")
    print(f"Best resistance: {float(best['resistance_ohms']):.6g} ohms")
    print(f"Best gain: {float(best['gain_at_1k_db']):.6f} dB")
    print(f"Error: {float(best['error_db']):.6f} dB")
    print(f"Trials CSV: {csv_path}")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
