#!/usr/bin/env python3
"""Run the Phase 4A coarse multi-objective mixed-signal DAQ optimization."""

from __future__ import annotations

from pathlib import Path

from examples.mixed_signal_daq_study import COMMON_REPORT_CONTEXT, TRANSIENT_ANALYSES
from mcp_server import (
    build_experiment_report,
    evaluate_optimization_study,
    generate_optimization_plan,
    run_optimization_experiment,
)


EXAMPLES_DIR = Path(__file__).resolve().parent

DESIGN_PARAMETERS = [
    {
        "name": "RAA1",
        "kind": "preferred_values",
        "series": "E12",
        "values": [820, 1000],
        "unit": "ohm",
    },
    {
        "name": "CAA1",
        "kind": "preferred_values",
        "series": "E12",
        "values": [82e-12, 100e-12],
        "unit": "F",
    },
    {
        "name": "CAA2",
        "kind": "preferred_values",
        "series": "E12",
        "values": [82e-12, 100e-12],
        "unit": "F",
    },
    {
        "name": "ROUT",
        "kind": "continuous",
        "minimum": 35,
        "maximum": 65,
        "count": 2,
        "unit": "ohm",
    },
]

FIXED_PARAMETERS = {
    "RAA2": 1000,
    "GAIN": 1.6,
    "CLOAD": 50e-12,
    "RSW": 25,
    "CHOLD": 47e-12,
}

CORNERS = [
    {
        "name": "adc_load",
        "parameter": "CADC",
        "unit": "F",
        "values": [
            {"name": "light", "value": 20e-12},
            {"name": "heavy", "value": 80e-12},
        ],
    }
]

OBJECTIVES = [
    {
        "name": "alias_gain",
        "experiment": "ac",
        "analysis": "analog_performance",
        "metric": "ac_gain_db",
        "goal": "minimize",
        "metric_parameters": {"frequency_value": 10_000_000},
    },
    {
        "name": "settling_time",
        "experiment": "transient",
        "analysis": "frontend_settling",
        "metric": "settling_time",
        "goal": "minimize",
    },
]

CONSTRAINTS = [
    {
        "name": "passband_gain",
        "experiment": "ac",
        "analysis": "analog_performance",
        "metric": "ac_gain_db",
        "operator": ">=",
        "target": 3.5,
        "metric_parameters": {"frequency_value": 100_000},
    },
    {
        "name": "minimum_bandwidth",
        "experiment": "ac",
        "analysis": "analog_performance",
        "metric": "cutoff_frequency",
        "operator": ">=",
        "target": 800_000,
        "metric_parameters": {"reference_frequency": 1000},
    },
    {
        "name": "maximum_bandwidth",
        "experiment": "ac",
        "analysis": "analog_performance",
        "metric": "cutoff_frequency",
        "operator": "<=",
        "target": 1_300_000,
        "metric_parameters": {"reference_frequency": 1000},
    },
    {
        "name": "passband_peaking",
        "experiment": "ac",
        "analysis": "analog_performance",
        "metric": "peaking_db",
        "operator": "<=",
        "target": 0.1,
        "metric_parameters": {"reference_frequency": 1000},
    },
    {
        "name": "alias_rejection",
        "experiment": "ac",
        "analysis": "analog_performance",
        "metric": "ac_gain_db",
        "operator": "<=",
        "target": -20,
        "metric_parameters": {"frequency_value": 10_000_000},
    },
    {
        "name": "frontend_settling",
        "experiment": "transient",
        "analysis": "frontend_settling",
        "metric": "settling_time",
        "operator": "<=",
        "target": 1.5e-6,
    },
    {
        "name": "tracking_upper",
        "experiment": "transient",
        "analysis": "track_error",
        "metric": "maximum",
        "operator": "<=",
        "target": 0.005,
    },
    {
        "name": "tracking_lower",
        "experiment": "transient",
        "analysis": "track_error",
        "metric": "minimum",
        "operator": ">=",
        "target": -0.005,
    },
    {
        "name": "hold_droop",
        "experiment": "transient",
        "analysis": "hold_droop",
        "metric": "ripple",
        "operator": "<=",
        "target": 0.005,
    },
]

AC_ANALYSES = [
    {
        "name": "analog_performance",
        "variable": "V(afe)",
        "secondary_variable": "V(in)",
        "requirements": [
            {
                "metric": "ac_gain_db",
                "operator": ">=",
                "target": 3.5,
                "frequency_value": 100_000,
            },
            {
                "metric": "ac_gain_db",
                "operator": "<=",
                "target": -20,
                "frequency_value": 10_000_000,
            },
            {
                "metric": "cutoff_frequency",
                "operator": ">=",
                "target": 800_000,
                "reference_frequency": 1000,
            },
            {
                "metric": "cutoff_frequency",
                "operator": "<=",
                "target": 1_300_000,
                "reference_frequency": 1000,
            },
            {
                "metric": "peaking_db",
                "operator": "<=",
                "target": 0.1,
                "reference_frequency": 1000,
            },
        ],
    }
]

REPORT_CONTEXT = {
    **COMMON_REPORT_CONTEXT,
    "title": "Phase 4A mixed-signal DAQ optimization",
    "circuit_summary": (
        "A two-pole anti-alias network, buffered gain stage, ADC driver, and "
        "clocked sample-and-hold expose the central DAQ tradeoff between "
        "out-of-band rejection and acquisition settling."
    ),
    "mcp_context": (
        "These runs prove that one immutable optimization candidate plan can "
        "drive both AC and transient LTspice experiments and retain metric-level "
        "evidence for Pareto selection."
    ),
}


def run_study() -> dict[str, object]:
    plan = generate_optimization_plan(
        DESIGN_PARAMETERS,
        OBJECTIVES,
        CONSTRAINTS,
        fixed_parameters=FIXED_PARAMETERS,
        corner_axes=CORNERS,
    )
    ac = run_optimization_experiment(
        plan["plan_id"],
        (EXAMPLES_DIR / "mixed_signal_daq_ac.cir").read_text(encoding="utf-8"),
        AC_ANALYSES,
        filename="mixed_signal_daq_ac.cir",
        reuse_cache=True,
    )
    transient = run_optimization_experiment(
        plan["plan_id"],
        (EXAMPLES_DIR / "mixed_signal_daq_transient.cir").read_text(
            encoding="utf-8"
        ),
        TRANSIENT_ANALYSES,
        filename="mixed_signal_daq_transient.cir",
        reuse_cache=True,
    )
    ac_report = build_experiment_report(
        ac["experiment_id"],
        {
            **REPORT_CONTEXT,
            "simulation_summary": (
                "A 1 kHz to 100 MHz AC sweep evaluates passband gain, cutoff, "
                "peaking, and 10 MHz alias rejection for every candidate and ADC corner."
            ),
        },
    )
    transient_report = build_experiment_report(
        transient["experiment_id"],
        {
            **REPORT_CONTEXT,
            "simulation_summary": (
                "A 2 µs acquisition transient evaluates frontend settling, "
                "tracking error, and held-voltage droop for the same immutable candidates."
            ),
        },
    )
    optimization = evaluate_optimization_study(
        plan["plan_id"],
        {
            "ac": ac["experiment_id"],
            "transient": transient["experiment_id"],
        },
    )
    return {
        "plan": plan,
        "ac": {"experiment": ac, "report": ac_report},
        "transient": {"experiment": transient, "report": transient_report},
        "optimization": optimization,
    }


def main() -> None:
    study = run_study()
    optimization = study["optimization"]
    print(
        f"Plan: {study['plan']['plan_id']} "
        f"({study['plan']['candidate_count']} candidates, "
        f"{study['plan']['point_count']} corner-expanded points)"
    )
    print(
        f"Selected candidate: {optimization['selected_candidate_index']} -- "
        f"{optimization['selection_explanation']}"
    )
    print(f"Pareto report: {optimization['report_html']}")


if __name__ == "__main__":
    main()
