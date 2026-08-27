#!/usr/bin/env python3
"""Refine a completed mixed-signal DAQ optimization through durable jobs."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import mcp_server
from examples.mixed_signal_daq_study import TRANSIENT_ANALYSES
from examples.optimize_mixed_signal_daq import AC_ANALYSES, REPORT_CONTEXT


EXAMPLES_DIR = Path(__file__).resolve().parent
REFINEMENT_REPORT_CONTEXT = {
    **REPORT_CONTEXT,
    "title": "Phase 4C mixed-signal DAQ local refinement",
    "mcp_context": (
        "These runs prove that a completed Pareto study can generate a bounded, "
        "provenance-linked child plan and execute it through the unchanged "
        "durable AC/transient pipeline."
    ),
}


def run_study(
    parent_study_id: str,
    *,
    max_candidates: int = 64,
    max_points: int = 256,
    reuse_cache: bool = True,
    poll_seconds: float = 0.1,
    timeout_seconds: float = 900,
) -> dict[str, object]:
    plan = mcp_server.generate_optimization_refinement_plan(
        parent_study_id,
        max_candidates,
        max_points,
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
            raise TimeoutError("durable DAQ refinement did not finish in time")
        time.sleep(poll_seconds)
        current = mcp_server.get_optimization_study(
            defined["optimization_job_id"]
        )
    if current["status"] != "completed":
        raise RuntimeError(
            f"durable DAQ refinement ended in {current['status']}: {current['error']}"
        )

    reports: dict[str, object] = {}
    for name, child in current["experiments"].items():
        summary = (
            "A 1 kHz to 100 MHz AC sweep evaluates passband gain, cutoff, "
            "peaking, and 10 MHz alias rejection for each refined candidate."
            if name == "ac"
            else (
                "A 2 µs acquisition transient evaluates settling, tracking "
                "error, and held-voltage droop for the same refined candidates."
            )
        )
        reports[name] = mcp_server.build_experiment_report(
            child["experiment_id"],
            {**REFINEMENT_REPORT_CONTEXT, "simulation_summary": summary},
        )
    return {"plan": plan, "job": current, "reports": reports}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parent_study_id")
    parser.add_argument("--max-candidates", type=int, default=64)
    parser.add_argument("--max-points", type=int, default=256)
    args = parser.parse_args()
    study = run_study(
        args.parent_study_id,
        max_candidates=args.max_candidates,
        max_points=args.max_points,
    )
    job = study["job"]
    print(
        f"Refinement plan: {study['plan']['plan_id']}\n"
        f"Durable job: {job['optimization_job_id']}\n"
        f"Optimization study: {job['optimization_study_id']}\n"
        f"Pareto report: {job['report_html']}"
    )


if __name__ == "__main__":
    main()
