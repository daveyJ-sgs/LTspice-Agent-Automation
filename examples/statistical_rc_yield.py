#!/usr/bin/env python3
"""Run a bounded RC yield study through the production statistical API."""

from __future__ import annotations

from mcp_server import (
    analyze_statistical_sensitivity,
    analyze_statistical_worst_cases,
    build_experiment_report,
    generate_statistical_plan,
    run_statistical_experiment,
    summarize_statistical_experiment,
)

SAMPLE_COUNT = 24
SEED = 20260825

NETLIST = """* Production statistical API RC yield example
V1 in 0 AC 1
R1 in out {R}
C1 out 0 {C}
.ac dec 100 10 1Meg
.end
"""

VARIABLES = [
    {
        "name": "R",
        "distribution": "gaussian",
        "nominal": "10000",
        "sigma": "500",
        "minimum": "8000",
        "maximum": "12000",
        "unit": "ohm",
    },
    {
        "name": "C",
        "distribution": "gaussian",
        "nominal": "1e-6",
        "sigma": "5e-8",
        "minimum": "8e-7",
        "maximum": "1.2e-6",
        "unit": "F",
    },
]

ANALYSES = [
    {
        "name": "gain_window",
        "variable": "V(out)",
        "secondary_variable": "V(in)",
        "requirements": [
            {
                "metric": "ac_gain_db",
                "operator": ">=",
                "target": -38.0,
                "frequency_value": 1000.0,
            },
            {
                "metric": "ac_gain_db",
                "operator": "<=",
                "target": -34.0,
                "frequency_value": 1000.0,
            },
        ],
    }
]


def main() -> None:
    plan = generate_statistical_plan(
        VARIABLES,
        SAMPLE_COUNT,
        SEED,
        sampling_method="halton",
    )
    experiment = run_statistical_experiment(
        plan["plan_id"],
        NETLIST,
        ANALYSES,
        reuse_cache=True,
    )
    experiment_id = experiment["experiment_id"]
    summary = summarize_statistical_experiment(experiment_id)
    worst_cases = analyze_statistical_worst_cases(experiment_id)
    sensitivity = analyze_statistical_sensitivity(experiment_id)
    report = build_experiment_report(experiment_id)

    observed = summary["observed_yield"]
    interval_low = summary["confidence_low"]
    interval_high = summary["confidence_high"]
    print(f"Experiment: {experiment_id}")
    print(f"Immutable plan: {plan['plan_id']} ({plan['sampling_method']})")
    print(
        "Yield: "
        + (
            "not available"
            if observed is None
            else f"{100 * observed:.2f}% "
            f"(Wilson 95% {100 * interval_low:.2f}%–{100 * interval_high:.2f}%)"
        )
    )
    print(f"Statistics: {summary['statistics_json']}")
    print(f"Worst cases: {worst_cases['worst_cases_json']}")
    print(f"Sensitivity: {sensitivity['sensitivity_json']}")
    print(f"Offline report: {report['report_html']}")


if __name__ == "__main__":
    main()
