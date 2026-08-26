from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from examples import statistical_rc_yield


class StatisticalYieldExampleTests(unittest.TestCase):
    def test_example_uses_the_production_statistical_pipeline(self) -> None:
        plan = {
            "plan_id": "statistical-plan-0123456789abcdef",
            "sampling_method": "halton",
        }
        experiment = {"experiment_id": "mcp-experiment-fixture"}
        summary = {
            "observed_yield": 0.75,
            "confidence_low": 0.5,
            "confidence_high": 0.9,
            "statistics_json": "/runs/statistics.json",
        }
        with (
            patch.object(
                statistical_rc_yield,
                "generate_statistical_plan",
                return_value=plan,
            ) as generate,
            patch.object(
                statistical_rc_yield,
                "run_statistical_experiment",
                return_value=experiment,
            ) as run,
            patch.object(
                statistical_rc_yield,
                "summarize_statistical_experiment",
                return_value=summary,
            ) as summarize,
            patch.object(
                statistical_rc_yield,
                "analyze_statistical_worst_cases",
                return_value={"worst_cases_json": "/runs/worst_cases.json"},
            ) as worst,
            patch.object(
                statistical_rc_yield,
                "analyze_statistical_sensitivity",
                return_value={"sensitivity_json": "/runs/sensitivity.json"},
            ) as sensitivity,
            patch.object(
                statistical_rc_yield,
                "build_experiment_report",
                return_value={"report_html": "/runs/report.html"},
            ) as report,
            redirect_stdout(io.StringIO()) as output,
        ):
            statistical_rc_yield.main()

        generate.assert_called_once_with(
            statistical_rc_yield.VARIABLES,
            statistical_rc_yield.SAMPLE_COUNT,
            statistical_rc_yield.SEED,
            sampling_method="halton",
        )
        run.assert_called_once_with(
            plan["plan_id"],
            statistical_rc_yield.NETLIST,
            statistical_rc_yield.ANALYSES,
            reuse_cache=True,
        )
        summarize.assert_called_once_with(experiment["experiment_id"])
        worst.assert_called_once_with(experiment["experiment_id"])
        sensitivity.assert_called_once_with(experiment["experiment_id"])
        report.assert_called_once_with(experiment["experiment_id"])
        self.assertIn("Yield: 75.00%", output.getvalue())
        self.assertIn("Offline report: /runs/report.html", output.getvalue())


if __name__ == "__main__":
    unittest.main()
