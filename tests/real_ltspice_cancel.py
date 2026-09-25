#!/usr/bin/env python3
"""Real-LTspice check that cancel and timeout stop the simulator process tree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import ltspice_wrapper
from ltspice_wrapper import SimulationCancelled, run_netlist

# Long enough that it cannot finish before the stop request: a 10 s
# transient with a 1 ns maximum step.
SLOW_DECK = (
    "* Long transient used to verify cancellation\n"
    "V1 in 0 SIN(0 1 1k)\n"
    "R1 in out 1k\nC1 out 0 100n\n"
    ".tran 0 10 0 1n\n.end\n"
)


def _simulator_processes() -> set[str]:
    """Return the process ids of running simulator executables."""
    name = ltspice_wrapper.LTSPICE.name
    if sys.platform == "win32":
        listing = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {name}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout
        return {
            line.split('","')[1]
            for line in listing.splitlines()
            if name.lower() in line.lower() and '","' in line
        }
    listing = subprocess.run(
        ["pgrep", "-f", str(ltspice_wrapper.LTSPICE)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return {line.strip() for line in listing.splitlines() if line.strip()}


def _wait_until_gone(ignored: set[str], deadline_seconds: float) -> set[str]:
    """Wait for simulators started by this check (not in ``ignored``) to exit."""
    deadline = time.monotonic() + deadline_seconds
    remaining = _simulator_processes() - ignored
    while remaining and time.monotonic() < deadline:
        time.sleep(0.25)
        remaining = _simulator_processes() - ignored
    return remaining


def main() -> None:
    evidence_value = os.environ.get("REAL_LTSPICE_EVIDENCE_DIR")
    if not evidence_value:
        raise RuntimeError("REAL_LTSPICE_EVIDENCE_DIR is required")
    evidence = Path(evidence_value) / "cancel-check"
    evidence.mkdir(parents=True, exist_ok=True)
    source = evidence / "slow_transient.cir"
    source.write_text(SLOW_DECK, encoding="utf-8")
    # An LTspice window the user already has open is not ours to judge.
    before = _simulator_processes()

    results: dict[str, object] = {}

    cancel = threading.Event()
    timer = threading.Timer(3.0, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        run_netlist(source, evidence / "cancelled", timeout_seconds=300, cancel_event=cancel)
    except SimulationCancelled:
        pass
    else:
        raise AssertionError("the slow transient finished before it was cancelled")
    finally:
        timer.cancel()
    cancel_seconds = time.monotonic() - started
    leftover = _wait_until_gone(before, 10)
    manifest = json.loads((evidence / "cancelled" / "run_manifest.json").read_text())
    if manifest["status"] != "cancelled":
        raise AssertionError(f"cancelled run manifest says {manifest['status']}")
    if leftover:
        raise AssertionError(f"LTspice still running after cancel: {leftover}")
    if cancel_seconds > 15:
        raise AssertionError(f"cancel took {cancel_seconds:.1f} s")
    results["cancel_seconds"] = round(cancel_seconds, 3)

    started = time.monotonic()
    try:
        run_netlist(source, evidence / "timed-out", timeout_seconds=3)
    except RuntimeError as exc:
        if "exceeded 3 seconds" not in str(exc):
            raise
    else:
        raise AssertionError("the slow transient finished inside a 3 s timeout")
    timeout_seconds = time.monotonic() - started
    leftover = _wait_until_gone(before, 10)
    manifest = json.loads((evidence / "timed-out" / "run_manifest.json").read_text())
    if manifest["status"] != "timeout":
        raise AssertionError(f"timed-out run manifest says {manifest['status']}")
    if leftover:
        raise AssertionError(f"LTspice still running after timeout: {leftover}")
    if timeout_seconds > 15:
        raise AssertionError(f"timeout took {timeout_seconds:.1f} s")
    results["timeout_seconds"] = round(timeout_seconds, 3)

    (evidence / "cancel_check.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(results))


if __name__ == "__main__":
    main()
