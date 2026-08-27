#!/usr/bin/env python3
"""Run the Phase 4D tolerance proof for coarse and refined DAQ finalists."""

from __future__ import annotations

import argparse
from pathlib import Path

import mcp_server
import optimization_engine
from examples.mixed_signal_daq_study import (
    COMMON_REPORT_CONTEXT,
    CORNERS,
    TRANSIENT_ANALYSES,
)
from examples.optimize_mixed_signal_daq import AC_ANALYSES, FIXED_PARAMETERS


EXAMPLES_DIR = Path(__file__).resolve().parent
SAMPLE_COUNT = 32
SEED = 20260827
PLATFORM_TOLERANCES = {
    "ac_gain_db": 0.05,
    "cutoff_frequency": 5000.0,
    "peaking_db": 0.01,
    "settling_time": 50e-9,
    "ripple": 0.0005,
    "maximum": 0.0005,
    "minimum": 0.0005,
}
CORRELATIONS = [
    {"variables": ["RAA1", "RAA2"], "matrix": [[1, 0.5], [0.5, 1]]},
    {"variables": ["CAA1", "CAA2"], "matrix": [[1, 0.4], [0.4, 1]]},
]

MODELS = {
    "RAA1": (0.01, 0.95, 1.05, "ohm"),
    "RAA2": (0.01, 0.95, 1.05, "ohm"),
    "CAA1": (0.05, 0.80, 1.20, "F"),
    "CAA2": (0.05, 0.80, 1.20, "F"),
    "GAIN": (0.005, 0.975, 1.025, "V/V"),
    "ROUT": (0.10, 0.50, 1.50, "ohm"),
    "CLOAD": (0.10, 0.50, 1.50, "F"),
    "RSW": (0.10, 0.50, 1.50, "ohm"),
    "CHOLD": (0.05, 0.80, 1.20, "F"),
}

REPORT_CONTEXT = {
    **COMMON_REPORT_CONTEXT,
    "title": "Phase 4D mixed-signal DAQ finalist qualification",
    "mcp_context": (
        "These runs apply one frozen manufacturing population to each nominal "
        "optimizer finalist, retain named ADC-load corners, and provide the "
        "paired AC/transient evidence used by the robust design decision."
    ),
}


def _candidate_parameters(study_id: str, candidate_index: int) -> dict[str, float]:
    result, _ = optimization_engine._load_verified_optimization_study(
        mcp_server.RUNS_DIR, study_id
    )
    candidates = result["candidates"]
    if not isinstance(candidates, list) or not 0 <= candidate_index < len(candidates):
        raise ValueError(f"candidate {candidate_index} does not exist in {study_id}")
    candidate = candidates[candidate_index]
    if not isinstance(candidate, dict) or not isinstance(candidate.get("parameters"), dict):
        raise ValueError("optimization finalist parameters are invalid")
    return {name: float(value) for name, value in candidate["parameters"].items()}


def _variables(study_id: str, candidate_index: int) -> list[dict[str, object]]:
    nominal = {
        **{name: float(value) for name, value in FIXED_PARAMETERS.items()},
        **_candidate_parameters(study_id, candidate_index),
    }
    return [
        {
            "name": name,
            "distribution": "gaussian",
            "nominal": nominal[name],
            "sigma": nominal[name] * model[0],
            "minimum": nominal[name] * model[1],
            "maximum": nominal[name] * model[2],
            "unit": model[3],
        }
        for name, model in MODELS.items()
    ]


def _finish(experiment_id: str, summary: str) -> dict[str, object]:
    statistics = mcp_server.summarize_statistical_experiment(experiment_id)
    worst_cases = mcp_server.analyze_statistical_worst_cases(experiment_id)
    sensitivity = mcp_server.analyze_statistical_sensitivity(experiment_id)
    report = mcp_server.build_experiment_report(
        experiment_id,
        {**REPORT_CONTEXT, "simulation_summary": summary},
        max_traces_per_plot=16,
    )
    return {
        "experiment_id": experiment_id,
        "statistics": statistics,
        "worst_cases": worst_cases,
        "sensitivity": sensitivity,
        "report": report,
    }


def run_study(
    coarse_study_id: str,
    refined_study_id: str,
    *,
    coarse_candidate_index: int = 15,
    refined_candidate_index: int = 7,
    sample_count: int = SAMPLE_COUNT,
    reuse_cache: bool = True,
) -> dict[str, object]:
    finalists = [
        {
            "label": "coarse-winner",
            "study_id": coarse_study_id,
            "candidate_index": coarse_candidate_index,
        },
        {
            "label": "refined-finalist",
            "study_id": refined_study_id,
            "candidate_index": refined_candidate_index,
        },
    ]
    plan = mcp_server.generate_robust_selection_plan(
        finalists,
        {
            "coarse-winner": _variables(coarse_study_id, coarse_candidate_index),
            "refined-finalist": _variables(refined_study_id, refined_candidate_index),
        },
        sample_count,
        SEED,
        correlations=CORRELATIONS,
        corner_axes=CORNERS,
        sampling_method="halton",
    )
    outputs: dict[str, dict[str, object]] = {}
    experiments: dict[str, dict[str, str]] = {}
    for label, statistical_plan_id in plan["statistical_plan_ids"].items():
        ac = mcp_server.run_statistical_experiment(
            statistical_plan_id,
            (EXAMPLES_DIR / "mixed_signal_daq_ac.cir").read_text(encoding="utf-8"),
            AC_ANALYSES,
            filename="mixed_signal_daq_ac.cir",
            reuse_cache=reuse_cache,
        )
        transient = mcp_server.run_statistical_experiment(
            statistical_plan_id,
            (EXAMPLES_DIR / "mixed_signal_daq_transient.cir").read_text(
                encoding="utf-8"
            ),
            TRANSIENT_ANALYSES,
            filename="mixed_signal_daq_transient.cir",
            reuse_cache=reuse_cache,
        )
        outputs[label] = {
            "ac": _finish(
                ac["experiment_id"],
                "A 1 kHz to 100 MHz AC sweep checks bandwidth, passband gain, "
                "peaking, and 10 MHz alias rejection for this finalist.",
            ),
            "transient": _finish(
                transient["experiment_id"],
                "A 2 µs acquisition transient checks settling, tracking error, "
                "and hold droop for the identical sample and ADC-load corner.",
            ),
        }
        experiments[label] = {
            "ac": ac["experiment_id"],
            "transient": transient["experiment_id"],
        }
    selection = mcp_server.evaluate_robust_selection_study(
        plan["plan_id"], experiments
    )
    return {"plan": plan, "experiments": outputs, "selection": selection}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coarse_study_id")
    parser.add_argument("refined_study_id")
    parser.add_argument("--samples", type=int, default=SAMPLE_COUNT)
    args = parser.parse_args()
    result = run_study(
        args.coarse_study_id,
        args.refined_study_id,
        sample_count=args.samples,
    )
    selection = result["selection"]
    print(
        f"Robust plan: {result['plan']['plan_id']}\n"
        f"Selection study: {selection['study_id']}\n"
        f"Selected finalist: {selection['selected_finalist']}\n"
        f"Report: {selection['report_html']}"
    )


if __name__ == "__main__":
    main()
