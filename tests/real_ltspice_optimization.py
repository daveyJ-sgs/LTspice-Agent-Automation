#!/usr/bin/env python3
"""Qualify durable DAQ optimization against the macOS baseline on Windows."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import ltspice_wrapper
import mcp_server
import optimization_comparison
from examples import optimize_mixed_signal_daq_durable


PROJECT_DIR = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_DIR / "tests" / "fixtures" / "phase4b_macos_optimization_baseline.json"


def _require_child(name: str, child: dict[str, object], evidence_dir: Path) -> dict[str, object]:
    if child["status"] != "completed":
        raise AssertionError(f"{name} child did not complete")
    if child["point_count"] != 32 or child["completed_points"] != 32:
        raise AssertionError(f"{name} child did not retain all 32 points")
    if child["error_points"] != 0:
        raise AssertionError(f"{name} child contains simulator or analysis errors")
    experiment_dir = Path(str(child["experiment_dir"]))
    raw_count = len(
        [
            path
            for path in experiment_dir.glob("point-*/attempt-*/*.raw")
            if not path.name.endswith(".op.raw")
        ]
    )
    manifest_count = len(
        list(experiment_dir.glob("point-*/attempt-*/run_manifest.json"))
    )
    if raw_count != 32 or manifest_count != 32:
        raise AssertionError(
            f"{name} evidence is incomplete: {raw_count} RAW, "
            f"{manifest_count} run manifests"
        )
    return {
        "experiment_id": child["experiment_id"],
        "experiment_relative_path": str(experiment_dir.relative_to(evidence_dir)),
        "completed_points": child["completed_points"],
        "error_points": child["error_points"],
        "raw_files": raw_count,
        "run_manifests": manifest_count,
    }


if __name__ == "__main__":
    evidence_value = os.environ.get("REAL_LTSPICE_OPTIMIZATION_EVIDENCE_DIR")
    if not evidence_value:
        raise RuntimeError("REAL_LTSPICE_OPTIMIZATION_EVIDENCE_DIR is required")
    evidence_dir = Path(evidence_value).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)

    ltspice_wrapper.RUNS_DIR = evidence_dir
    mcp_server.RUNS_DIR = evidence_dir
    mcp_server._experiment_manager = None
    mcp_server._optimization_study_manager = None
    try:
        study = optimize_mixed_signal_daq_durable.run_study(
            reuse_cache=False, poll_seconds=0.1, timeout_seconds=600
        )
        job = study["job"]
        if job["status"] != "completed":
            raise AssertionError("durable optimization job did not complete")
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        if job["plan_id"] != baseline["plan_id"]:
            raise AssertionError(f"unexpected frozen plan: {job['plan_id']}")
        result_path = Path(str(job["results_json"]))
        result = json.loads(result_path.read_text(encoding="utf-8"))
        comparison = optimization_comparison.write_optimization_comparison(
            evidence_dir,
            baseline,
            result,
            optimize_mixed_signal_daq_durable.PLATFORM_TOLERANCES,
            baseline_label="macOS LTspice 17.2.4",
            candidate_label="Windows LTspice 26.0.2",
        )
        if not comparison["passed"]:
            raise AssertionError(
                "Windows optimization differs from the macOS baseline: "
                f"{comparison['comparison_json']}"
            )
        if result["selected_candidate_index"] != baseline["selected_candidate_index"]:
            raise AssertionError("platform selected a different DAQ candidate")

        qualification = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "simulator": str(ltspice_wrapper.LTSPICE),
            "plan_id": job["plan_id"],
            "optimization_job_id": job["optimization_job_id"],
            "optimization_study_id": job["optimization_study_id"],
            "selected_candidate_index": result["selected_candidate_index"],
            "candidate_count": result["candidate_count"],
            "feasible_candidates": result["feasible_candidates"],
            "constraint_failed_candidates": result["constraint_failed_candidates"],
            "pareto_candidates": result["pareto_candidates"],
            "children": {
                name: _require_child(name, child, evidence_dir)
                for name, child in job["experiments"].items()
            },
            "comparison": {
                **comparison,
                "comparison_dir": str(
                    Path(comparison["comparison_dir"]).relative_to(evidence_dir)
                ),
                "comparison_json": str(
                    Path(comparison["comparison_json"]).relative_to(evidence_dir)
                ),
                "report_html": str(
                    Path(comparison["report_html"]).relative_to(evidence_dir)
                ),
            },
        }
        summary = evidence_dir / "optimization_qualification_summary.json"
        summary.write_text(
            json.dumps(qualification, indent=2, sort_keys=True, allow_nan=False)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps(qualification, indent=2, sort_keys=True))
    finally:
        if mcp_server._experiment_manager is not None:
            mcp_server._experiment_manager.shutdown()
            mcp_server._experiment_manager = None
        mcp_server._optimization_study_manager = None
