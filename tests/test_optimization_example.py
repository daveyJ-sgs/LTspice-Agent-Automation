from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from examples import (
    optimize_mixed_signal_daq,
    optimize_mixed_signal_daq_durable,
    refine_mixed_signal_daq,
)


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

    def test_durable_daq_example_composes_and_polls_existing_jobs(self) -> None:
        plan = {"plan_id": "optimization-plan-fixture"}
        defined = {"optimization_job_id": "optimization-job-fixture"}
        queued = {**defined, "status": "queued"}
        completed = {
            **defined,
            "status": "completed",
            "error": None,
            "optimization_study_id": "optimization-study-fixture",
            "report_html": "/runs/optimization/report.html",
            "experiments": {
                "ac": {"experiment_id": "mcp-experiment-ac"},
                "transient": {"experiment_id": "mcp-experiment-transient"},
            },
        }
        with (
            patch.object(
                optimize_mixed_signal_daq_durable.mcp_server,
                "generate_optimization_plan",
                return_value=plan,
            ),
            patch.object(
                optimize_mixed_signal_daq_durable.mcp_server,
                "define_optimization_study",
                return_value=defined,
            ) as define,
            patch.object(
                optimize_mixed_signal_daq_durable.mcp_server,
                "start_optimization_study",
                return_value=queued,
            ),
            patch.object(
                optimize_mixed_signal_daq_durable.mcp_server,
                "get_optimization_study",
                return_value=completed,
            ),
            patch.object(
                optimize_mixed_signal_daq_durable.mcp_server,
                "build_experiment_report",
                side_effect=[{"report_html": "ac"}, {"report_html": "transient"}],
            ) as report,
            patch.object(optimize_mixed_signal_daq_durable.time, "sleep"),
        ):
            result = optimize_mixed_signal_daq_durable.run_study()

        self.assertEqual(define.call_args.args[0], plan["plan_id"])
        self.assertEqual(set(define.call_args.args[1]), {"ac", "transient"})
        self.assertEqual(report.call_count, 2)
        self.assertEqual(result["job"], completed)

    def test_daq_refinement_example_reuses_durable_children(self) -> None:
        parent_study_id = "optimization-study-parent"
        plan = {"plan_id": "optimization-plan-refined"}
        defined = {"optimization_job_id": "optimization-job-refined"}
        completed = {
            **defined,
            "status": "completed",
            "error": None,
            "optimization_study_id": "optimization-study-refined",
            "report_html": "/runs/refined/report.html",
            "experiments": {
                "ac": {"experiment_id": "mcp-experiment-refined-ac"},
                "transient": {
                    "experiment_id": "mcp-experiment-refined-transient"
                },
            },
        }
        with (
            patch.object(
                refine_mixed_signal_daq.mcp_server,
                "generate_optimization_refinement_plan",
                return_value=plan,
            ) as generate,
            patch.object(
                refine_mixed_signal_daq.mcp_server,
                "define_optimization_study",
                return_value=defined,
            ) as define,
            patch.object(
                refine_mixed_signal_daq.mcp_server,
                "start_optimization_study",
                return_value=completed,
            ),
            patch.object(
                refine_mixed_signal_daq.mcp_server,
                "build_experiment_report",
                side_effect=[{"report_html": "ac"}, {"report_html": "transient"}],
            ) as report,
        ):
            result = refine_mixed_signal_daq.run_study(
                parent_study_id,
                max_candidates=8,
                max_points=16,
            )

        generate.assert_called_once_with(parent_study_id, 8, 16)
        self.assertEqual(define.call_args.args[0], plan["plan_id"])
        self.assertEqual(set(define.call_args.args[1]), {"ac", "transient"})
        self.assertEqual(report.call_count, 2)
        self.assertEqual(result["job"], completed)


if __name__ == "__main__":
    unittest.main()
