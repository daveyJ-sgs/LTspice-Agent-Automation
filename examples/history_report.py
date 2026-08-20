#!/usr/bin/env python3
"""Print the accumulated RC sweep history."""

from __future__ import annotations

import sqlite3

from ltspice_wrapper import RUNS_DIR


def main() -> None:
    database_path = RUNS_DIR / "history.sqlite3"
    if not database_path.is_file():
        print(f"No history database found: {database_path}")
        return

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT sweep_id, created_at, resistance_ohms, gain_at_1k_db
            FROM rc_sweep_history
            ORDER BY rowid
            """
        ).fetchall()

    print(f"History database: {database_path}")
    print(f"Rows: {len(rows)}")
    for sweep_id, created_at, resistance, gain in rows:
        print(f"{created_at}  {sweep_id}  R={resistance} ohms  gain={gain:.4f} dB")

    with sqlite3.connect(database_path) as connection:
        monte_carlo_rows = connection.execute(
            """
            SELECT run_id, created_at, samples, passed, mean_gain_db, stdev_gain_db
            FROM monte_carlo_history
            ORDER BY rowid
            """
        ).fetchall() if _table_exists(connection, "monte_carlo_history") else []
    if monte_carlo_rows:
        print("Monte Carlo history:")
        for run_id, created_at, samples, passed, mean_gain, stdev_gain in monte_carlo_rows:
            print(
                f"{created_at}  {run_id}  yield={passed}/{samples} "
                f"mean/stdev={mean_gain:.4f}/{stdev_gain:.4f} dB"
            )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


if __name__ == "__main__":
    main()
