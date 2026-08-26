#!/usr/bin/env python3
"""Run the complete structured DAQ study with a real LTspice installation."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import ltspice_wrapper
import mcp_server
from examples import mixed_signal_daq_study


def _require_complete(
    label: str,
    outputs: dict[str, object],
    evidence_dir: Path,
) -> dict[str, object]:
    experiment = outputs["experiment"]
    summary = outputs["summary"]
    if experiment["status"] != "completed":
        raise AssertionError(f"{label} experiment did not complete")
    if experiment["point_count"] != 24 or experiment["completed_points"] != 24:
        raise AssertionError(f"{label} experiment did not retain all 24 points")
    if experiment["error_points"] != 0:
        raise AssertionError(f"{label} experiment contains simulator errors")
    if summary["invalid_points"] != 0:
        raise AssertionError(f"{label} experiment contains invalid evidence")

    corner_yields = {
        next(iter(corner["corners"].values())): corner["observed_yield"]
        for corner in summary["corner_results"]
    }
    if corner_yields != {"light": 1.0, "heavy": 1.0}:
        raise AssertionError(f"unexpected {label} corner yields: {corner_yields}")

    experiment_dir = Path(experiment["experiment_dir"])
    required = (
        "experiment_manifest.json",
        "results.json",
        "results.csv",
        "statistics.json",
        "statistics.csv",
        "worst_cases.json",
        "worst_cases.csv",
        "sensitivity.json",
        "sensitivity.csv",
        "report.html",
    )
    missing = [name for name in required if not (experiment_dir / name).is_file()]
    if missing:
        raise AssertionError(f"{label} evidence is incomplete: {missing}")
    raw_count = len(
        [
            path
            for path in experiment_dir.glob("point-*/*.raw")
            if not path.name.endswith(".op.raw")
        ]
    )
    manifest_count = len(list(experiment_dir.glob("point-*/**/run_manifest.json")))
    if raw_count != 24 or manifest_count != 24:
        raise AssertionError(
            f"{label} point evidence is incomplete: "
            f"{raw_count} RAW files, {manifest_count} manifests"
        )

    return {
        "experiment_id": experiment["experiment_id"],
        "point_count": experiment["point_count"],
        "completed_points": experiment["completed_points"],
        "error_points": experiment["error_points"],
        "invalid_points": summary["invalid_points"],
        "corner_yields": corner_yields,
        "report_html": str(
            Path(outputs["report"]["report_html"]).relative_to(evidence_dir)
        ),
        "raw_files": raw_count,
        "run_manifests": manifest_count,
    }


if __name__ == "__main__":
    evidence_dir_value = os.environ.get("REAL_LTSPICE_DAQ_EVIDENCE_DIR")
    if not evidence_dir_value:
        raise RuntimeError("REAL_LTSPICE_DAQ_EVIDENCE_DIR is required")
    evidence_dir = Path(evidence_dir_value).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)

    ltspice_wrapper.RUNS_DIR = evidence_dir
    mcp_server.RUNS_DIR = evidence_dir
    mcp_server._experiment_manager = None

    study = mixed_signal_daq_study.run_study()
    if study["plan"]["point_count"] != 24:
        raise AssertionError("DAQ statistical plan must contain 24 corner points")

    qualification = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "simulator": str(ltspice_wrapper.LTSPICE),
        "plan_id": study["plan"]["plan_id"],
        "plan_points": study["plan"]["point_count"],
        "ac": _require_complete("AC", study["ac"], evidence_dir),
        "transient": _require_complete(
            "transient", study["transient"], evidence_dir
        ),
    }
    summary_path = evidence_dir / "daq_qualification_summary.json"
    summary_path.write_text(
        json.dumps(qualification, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(qualification, indent=2, sort_keys=True))
