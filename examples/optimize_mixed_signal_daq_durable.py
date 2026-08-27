#!/usr/bin/env python3
"""Run the Phase 4B mixed-signal DAQ optimization through durable jobs."""

from __future__ import annotations

import time
from pathlib import Path

import mcp_server
from examples.optimize_mixed_signal_daq import (
    AC_ANALYSES,
    CONSTRAINTS,
    CORNERS,
    DESIGN_PARAMETERS,
    FIXED_PARAMETERS,
    OBJECTIVES,
    REPORT_CONTEXT,
)
from examples.mixed_signal_daq_study import TRANSIENT_ANALYSES


EXAMPLES_DIR = Path(__file__).resolve().parent
PLATFORM_TOLERANCES = {
    "alias_gain": {"absolute": 0.05, "relative": 0.0},
    "settling_time": {"absolute": 50e-9, "relative": 0.0},
}
PORTABLE_OBJECTIVES = [
    {
        **objective,
        "absolute_tolerance": PLATFORM_TOLERANCES[objective["name"]]["absolute"],
        "relative_tolerance": PLATFORM_TOLERANCES[objective["name"]]["relative"],
    }
    for objective in OBJECTIVES
]


def run_study(
    *,
    reuse_cache: bool = True,
    poll_seconds: float = 0.1,
    timeout_seconds: float = 900,
) -> dict[str, object]:
    plan = mcp_server.generate_optimization_plan(
        DESIGN_PARAMETERS,
        PORTABLE_OBJECTIVES,
        CONSTRAINTS,
        fixed_parameters=FIXED_PARAMETERS,
        corner_axes=CORNERS,
    )
    defined = mcp_server.define_optimization_study(
        plan["plan_id"],
        {
            "ac": {
                "netlist_template": (
                    EXAMPLES_DIR / "mixed_signal_daq_ac.cir"
                ).read_text(encoding="utf-8"),
                "waveform_analyses": AC_ANALYSES,
                "filename": "mixed_signal_daq_ac.cir",
                "reuse_cache": reuse_cache,
            },
            "transient": {
                "netlist_template": (
                    EXAMPLES_DIR / "mixed_signal_daq_transient.cir"
                ).read_text(encoding="utf-8"),
                "waveform_analyses": TRANSIENT_ANALYSES,
                "filename": "mixed_signal_daq_transient.cir",
                "reuse_cache": reuse_cache,
            },
        },
    )
    current = mcp_server.start_optimization_study(
        defined["optimization_job_id"]
    )
    deadline = time.monotonic() + timeout_seconds
    while current["status"] not in {"completed", "cancelled", "failed"}:
        if time.monotonic() >= deadline:
            mcp_server.cancel_optimization_study(defined["optimization_job_id"])
            raise TimeoutError("durable DAQ optimization did not finish in time")
        time.sleep(poll_seconds)
        current = mcp_server.get_optimization_study(
            defined["optimization_job_id"]
        )
    if current["status"] != "completed":
        raise RuntimeError(
            f"durable DAQ optimization ended in {current['status']}: {current['error']}"
        )

    reports: dict[str, object] = {}
    for name, child in current["experiments"].items():
        summary = (
            "A 1 kHz to 100 MHz AC sweep evaluates passband gain, cutoff, "
            "peaking, and 10 MHz alias rejection for every candidate and ADC corner."
            if name == "ac"
            else (
                "A 2 µs acquisition transient evaluates frontend settling, "
                "tracking error, and held-voltage droop for the same candidates."
            )
        )
        reports[name] = mcp_server.build_experiment_report(
            child["experiment_id"],
            {**REPORT_CONTEXT, "simulation_summary": summary},
        )
    return {"plan": plan, "job": current, "reports": reports}


def main() -> None:
    study = run_study()
    job = study["job"]
    print(
        f"Durable job: {job['optimization_job_id']}\n"
        f"Optimization study: {job['optimization_study_id']}\n"
        f"Pareto report: {job['report_html']}"
    )


if __name__ == "__main__":
    main()
