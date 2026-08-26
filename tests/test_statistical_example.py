from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from examples import mixed_signal_daq_study, statistical_rc_yield


class StatisticalYieldExampleTests(unittest.TestCase):
    @staticmethod
    def _schematic_values(text: str) -> dict[str, str]:
        values: dict[str, str] = {}
        instance = None
        for line in text.splitlines():
            if line.startswith("SYMBOL "):
                instance = None
            elif line.startswith("SYMATTR InstName "):
                instance = line.removeprefix("SYMATTR InstName ")
            elif line.startswith("SYMATTR Value ") and instance is not None:
                values[instance] = line.removeprefix("SYMATTR Value ")
        return values

    @staticmethod
    def _netlist_values(text: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in text.splitlines():
            fields = line.split()
            if not fields or fields[0][0] in "*.":
                continue
            instance = fields[0]
            prefix = instance[0].upper()
            value_index = {"R": 3, "C": 3, "V": 3, "E": 5, "S": 5, "B": 3}.get(
                prefix
            )
            if value_index is not None:
                values[instance] = " ".join(fields[value_index:])
        return values

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

    def test_mixed_signal_daq_templates_and_production_pipeline(self) -> None:
        expected = {
            *(variable["name"] for variable in mixed_signal_daq_study.VARIABLES),
            "CADC",
        }
        for filename in ("mixed_signal_daq_ac.cir", "mixed_signal_daq_transient.cir"):
            text = (mixed_signal_daq_study.EXAMPLES_DIR / filename).read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                set(re.findall(r"\{([A-Z][A-Z0-9_]*)\}", text)), expected
            )
        boundary = (
            mixed_signal_daq_study.EXAMPLES_DIR / "mixed_signal_daq_boundary.cir"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            set(re.findall(r"\{([A-Z][A-Z0-9_]*)\}", boundary)), {"TTRACK"}
        )

        plan = {"plan_id": "statistical-plan-daq", "point_count": 24}
        experiments = [
            {"experiment_id": "mcp-experiment-ac"},
            {"experiment_id": "mcp-experiment-transient"},
        ]
        outputs = {
            "summary": {
                "observed_yield": None,
                "corner_results": [
                    {"corners": {"adc_load": "light"}, "observed_yield": 1.0},
                    {"corners": {"adc_load": "heavy"}, "observed_yield": 0.5},
                ],
            },
            "report": {"report_html": "/runs/report.html"},
        }
        with (
            patch.object(
                mixed_signal_daq_study,
                "generate_statistical_plan",
                return_value=plan,
            ) as generate,
            patch.object(
                mixed_signal_daq_study,
                "run_statistical_experiment",
                side_effect=experiments,
            ) as run,
            patch.object(
                mixed_signal_daq_study, "_finish", return_value=outputs
            ) as finish,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = mixed_signal_daq_study.run_study()

        generate.assert_called_once_with(
            mixed_signal_daq_study.VARIABLES,
            mixed_signal_daq_study.SAMPLE_COUNT,
            mixed_signal_daq_study.SEED,
            correlations=mixed_signal_daq_study.CORRELATIONS,
            corner_axes=mixed_signal_daq_study.CORNERS,
            sampling_method="halton",
        )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            [call.kwargs["filename"] for call in run.call_args_list],
            ["mixed_signal_daq_ac.cir", "mixed_signal_daq_transient.cir"],
        )
        self.assertEqual(
            [call.args[0] for call in finish.call_args_list],
            ["mcp-experiment-ac", "mcp-experiment-transient"],
        )
        self.assertEqual(result["plan"], plan)
        self.assertEqual(result["ac"]["experiment"], experiments[0])
        self.assertEqual(result["transient"]["experiment"], experiments[1])

        with (
            patch.object(mixed_signal_daq_study, "run_study", return_value=result),
            redirect_stdout(io.StringIO()) as output,
        ):
            mixed_signal_daq_study.main()
        self.assertIn("light=100.00%, heavy=50.00%", output.getvalue())

    def test_mixed_signal_daq_schematic_matches_transient_inventory(self) -> None:
        schematic = (
            mixed_signal_daq_study.EXAMPLES_DIR / "mixed_signal_daq.asc"
        ).read_text(encoding="utf-8")
        transient = (
            mixed_signal_daq_study.EXAMPLES_DIR / "mixed_signal_daq_transient.cir"
        ).read_text(encoding="utf-8")

        self.assertEqual(
            self._schematic_values(schematic), self._netlist_values(transient)
        )
        self.assertEqual(
            set(
                re.findall(
                    r"^FLAG \d+ \d+ ([A-Za-z][A-Za-z0-9_]*)$",
                    schematic,
                    re.M,
                )
            ),
            {
                "aa1",
                "afe",
                "clk",
                "drive",
                "hold",
                "in",
                "n1",
                "n1buf",
                "n2",
                "sample_error",
            },
        )
        self.assertIn(
            ".model SAMPLE SW(Ron={RSW} Roff=1G Vt=1.65 Vh=0.1)", schematic
        )
        self.assertIn(".tran 0 2u 0 2n", schematic)


if __name__ == "__main__":
    unittest.main()
