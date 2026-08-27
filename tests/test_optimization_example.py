from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from examples import optimize_mixed_signal_daq


class OptimizationExampleTests(unittest.TestCase):
    def test_daq_example_uses_one_plan_for_ac_transient_and_pareto_evidence(
        self,
    ) -> None:
        expected_parameters = {
            *(parameter["name"] for parameter in optimize_mixed_signal_daq.DESIGN_PARAMETERS),
            *optimize_mixed_signal_daq.FIXED_PARAMETERS,
            "CADC",
        }
        for filename in ("mixed_signal_daq_ac.cir", "mixed_signal_daq_transient.cir"):
            netlist = (optimize_mixed_signal_daq.EXAMPLES_DIR / filename).read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                set(re.findall(r"\{([A-Z][A-Z0-9_]*)\}", netlist)),
                expected_parameters,
            )
        plan = {
            "plan_id": "optimization-plan-fixture",
            "candidate_count": 16,
            "point_count": 32,
        }
        experiments = [
            {"experiment_id": "mcp-experiment-ac"},
            {"experiment_id": "mcp-experiment-transient"},
        ]
        optimization = {
            "selected_candidate_index": 7,
            "selection_explanation": "Candidate 7 selected.",
            "report_html": "/runs/optimization-report.html",
        }
        with (
            patch.object(
                optimize_mixed_signal_daq,
                "generate_optimization_plan",
                return_value=plan,
            ) as generate,
            patch.object(
                optimize_mixed_signal_daq,
                "run_optimization_experiment",
                side_effect=experiments,
            ) as run,
            patch.object(
                optimize_mixed_signal_daq,
                "build_experiment_report",
                side_effect=[
                    {"report_html": "/runs/ac/report.html"},
                    {"report_html": "/runs/transient/report.html"},
                ],
            ) as report,
            patch.object(
                optimize_mixed_signal_daq,
                "evaluate_optimization_study",
                return_value=optimization,
            ) as evaluate,
        ):
            result = optimize_mixed_signal_daq.run_study()

        generate.assert_called_once_with(
            optimize_mixed_signal_daq.DESIGN_PARAMETERS,
            optimize_mixed_signal_daq.OBJECTIVES,
            optimize_mixed_signal_daq.CONSTRAINTS,
            fixed_parameters=optimize_mixed_signal_daq.FIXED_PARAMETERS,
            corner_axes=optimize_mixed_signal_daq.CORNERS,
        )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [plan["plan_id"], plan["plan_id"]],
        )
        self.assertEqual(
            [call.kwargs["filename"] for call in run.call_args_list],
            ["mixed_signal_daq_ac.cir", "mixed_signal_daq_transient.cir"],
        )
        self.assertEqual(report.call_count, 2)
        evaluate.assert_called_once_with(
            plan["plan_id"],
            {
                "ac": "mcp-experiment-ac",
                "transient": "mcp-experiment-transient",
            },
        )
        self.assertEqual(result["optimization"], optimization)


if __name__ == "__main__":
    unittest.main()
