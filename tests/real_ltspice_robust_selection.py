#!/usr/bin/env python3
"""Run Phase 4D finalist yield proof with real Windows LTspice."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import ltspice_wrapper
import mcp_server
import robust_selection
from examples import qualify_mixed_signal_daq_finalists, refine_mixed_signal_daq


PROJECT_DIR = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_DIR / "tests" / "fixtures" / "phase4d_macos_robust_baseline.json"


def _evidence_count(experiment_id: str, evidence_dir: Path) -> dict[str, object]:
    experiment_dir = evidence_dir / experiment_id
    manifest = json.loads(
        (experiment_dir / "experiment_manifest.json").read_text(encoding="utf-8")
    )
    if (
        manifest["status"] != "completed"
        or manifest["point_count"] != 64
        or manifest["completed_points"] != 64
        or manifest["error_points"] != 0
    ):
        raise AssertionError(f"incomplete robust experiment: {experiment_id}")
    raw_files = len(
        [
            path
            for path in experiment_dir.glob("point-*/attempt-*/*.raw")
            if not path.name.endswith(".op.raw")
        ]
    )
    run_manifests = len(
        list(experiment_dir.glob("point-*/attempt-*/run_manifest.json"))
    )
    if raw_files != 64 or run_manifests != 64:
        raise AssertionError(
            f"incomplete robust evidence for {experiment_id}: "
            f"{raw_files} RAW, {run_manifests} manifests"
        )
    return {
        "experiment_id": experiment_id,
        "raw_files": raw_files,
        "run_manifests": run_manifests,
    }


if __name__ == "__main__":
    evidence_value = os.environ.get("REAL_LTSPICE_OPTIMIZATION_EVIDENCE_DIR")
    if not evidence_value:
        raise RuntimeError("REAL_LTSPICE_OPTIMIZATION_EVIDENCE_DIR is required")
    evidence_dir = Path(evidence_value).resolve()
    coarse_summary = json.loads(
        (evidence_dir / "optimization_qualification_summary.json").read_text(
            encoding="utf-8"
        )
    )
    coarse_study_id = coarse_summary["optimization_study_id"]

    ltspice_wrapper.RUNS_DIR = evidence_dir
    mcp_server.RUNS_DIR = evidence_dir
    mcp_server._experiment_manager = None
    mcp_server._optimization_study_manager = None
    try:
        refinement = refine_mixed_signal_daq.run_study(
            coarse_study_id,
            max_candidates=64,
            max_points=256,
            reuse_cache=False,
            poll_seconds=0.1,
            timeout_seconds=900,
        )
        refined_study_id = refinement["job"]["optimization_study_id"]
        qualification = qualify_mixed_signal_daq_finalists.run_study(
            coarse_study_id,
            refined_study_id,
            reuse_cache=False,
        )
        selection_path = Path(qualification["selection"]["results_json"])
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        candidate_summary = robust_selection.portability_summary(selection)
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        comparison = robust_selection.write_portability_comparison(
            evidence_dir,
            baseline,
            candidate_summary,
            qualify_mixed_signal_daq_finalists.PLATFORM_TOLERANCES,
            baseline_label="macOS LTspice 17.2.4",
            candidate_label="Windows LTspice 26.0.2",
        )
        if not comparison["passed"]:
            raise AssertionError(
                "Windows robust selection differs from macOS: "
                f"{comparison['comparison_json']}"
            )
        evidence = {
            label: {
                name: _evidence_count(outputs["experiment_id"], evidence_dir)
                for name, outputs in studies.items()
            }
            for label, studies in qualification["experiments"].items()
        }
        summary = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "simulator": str(ltspice_wrapper.LTSPICE),
            "coarse_study_id": coarse_study_id,
            "refined_study_id": refined_study_id,
            "robust_plan_id": qualification["plan"]["plan_id"],
            "robust_study_id": qualification["selection"]["study_id"],
            "selected_finalist": selection["selected_finalist"],
            "portability_signature": selection["portability_signature"],
            "experiments": evidence,
            "comparison": comparison,
        }
        summary_path = evidence_dir / "robust_selection_qualification_summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        if mcp_server._experiment_manager is not None:
            mcp_server._experiment_manager.shutdown()
            mcp_server._experiment_manager = None
        mcp_server._optimization_study_manager = None
