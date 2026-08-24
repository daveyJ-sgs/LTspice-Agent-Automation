from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import experiment_visualization
import experiment_index
from raw_parser import RawData


class ExperimentVisualizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary_directory.name) / "runs"
        self.runs.mkdir()
        self.baseline_id = "mcp-experiment-20260824-180000-000000-a1b2c3d4"
        self.candidate_id = "mcp-experiment-20260824-180100-000000-b1c2d3e4"
        self._write_experiment(self.baseline_id, gain=-2.0, passed=True)
        self._write_experiment(self.candidate_id, gain=-4.0, passed=False)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_experiment(self, experiment_id: str, *, gain: float, passed: bool) -> None:
        directory = self.runs / experiment_id
        batch = directory / "native-batch"
        batch.mkdir(parents=True)
        raw_path = batch / "circuit.raw"
        raw_path.write_bytes(b"raw-placeholder")
        definition = {
            "netlist_template": "R1 in out {R}\n.end\n",
            "parameters": [{"name": "R", "values": ["1k"], "unit": "ohm"}],
            "derived_parameters": [],
            "parameter_order": ["R"],
            "derived_parameter_order": [],
            "parameter_units": {"R": "ohm"},
            "execution_mode": "native",
            "reuse_cache": False,
        }
        manifest = {
            "schema_version": 2,
            "engine_version": 1,
            "experiment_id": experiment_id,
            "status": "completed",
            "definition": definition,
            "point_count": 1,
            "finished_points": 1,
            "completed_points": 1,
            "error_points": 0,
            "passed_points": int(passed),
            "failed_points": int(not passed),
            "all_passed": passed,
            "created_at": "2026-08-24T18:00:00-07:00",
            "updated_at": "2026-08-24T18:00:01-07:00",
            "definition_hash": experiment_index._definition_hash(definition),
        }
        requirement = {
            "metric": "ac_gain_db",
            "value": gain,
            "unit": "dB",
            "threshold": {"operator": ">=", "target": -3.0, "unit": "dB"},
            "passed": passed,
            "parameters": {"frequency_value": 1000.0},
            "evidence": {"frequency": 1000.0},
        }
        analysis = {
            "name": "gain <script>alert(1)</script>",
            "status": "completed",
            "error": None,
            "analysis": {
                "all_passed": passed,
                "analysis_resolution": "full",
                "axis_variable": "frequency",
                "raw_file": str(raw_path),
                "results": [requirement],
                "secondary_variable": "V(in)",
                "source_points": 4,
                "step_index": 0,
                "variable": "V(out)",
            },
        }
        point = {
            "index": 0,
            "parameters": {"R": "1k"},
            "run_dir": str(batch),
            "simulation_status": "completed",
            "duration_seconds": 0.1,
            "measurements": {"gain": gain},
            "analyses": [analysis],
            "all_passed": passed,
            "error": None,
            "native_step_index": 0,
        }
        results = {
            "experiment_id": experiment_id,
            "status": "completed",
            "execution_mode": "native",
            "parameter_order": ["R"],
            "derived_parameter_order": [],
            "parameter_units": {"R": "ohm"},
            "point_count": 1,
            "completed_points": 1,
            "error_points": 0,
            "passed_points": int(passed),
            "failed_points": int(not passed),
            "all_passed": passed,
            "points": [point],
            "native_batch": None,
        }
        (directory / "experiment_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (directory / "results.json").write_text(
            json.dumps(results), encoding="utf-8"
        )

    def _add_empty_candidate_point(self) -> None:
        directory = self.runs / self.candidate_id
        manifest_path = directory / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["definition"]["parameters"][0]["values"].append("2k")
        manifest["definition_hash"] = experiment_index._definition_hash(
            manifest["definition"]
        )
        manifest["point_count"] = 2
        manifest["finished_points"] = 2
        manifest["completed_points"] = 2
        manifest["passed_points"] = 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        results_path = directory / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["point_count"] = 2
        results["completed_points"] = 2
        results["passed_points"] = 1
        results["points"].append(
            {
                "index": 1,
                "parameters": {"R": "2k"},
                "run_dir": str(directory / "native-batch"),
                "simulation_status": "completed",
                "duration_seconds": None,
                "measurements": {},
                "analyses": [],
                "all_passed": True,
                "error": None,
                "native_step_index": 1,
            }
        )
        results_path.write_text(json.dumps(results), encoding="utf-8")

    @staticmethod
    def _raw_data(scale: float = 1.0) -> RawData:
        return RawData(
            flags="complex",
            variables=["frequency", "V(out)", "V(in)"],
            values={
                "frequency": [complex(value, 0) for value in (10, 100, 1000, 10000)],
                "V(out)": [complex(scale * value, 0) for value in (1, 0.9, 0.5, 0.1)],
                "V(in)": [complex(1, 0)] * 4,
            },
        )

    def test_builds_deterministic_comparison_with_raw_overlays_and_markers(self) -> None:
        self._add_empty_candidate_point()
        for experiment_id in (self.baseline_id, self.candidate_id):
            (self.runs / experiment_id / "report.html").write_text(
                "report", encoding="utf-8"
            )
        with patch.object(
            experiment_visualization.experiment_report.raw_parser,
            "parse_raw",
            side_effect=[self._raw_data(), self._raw_data(0.8)],
        ):
            result = experiment_visualization.build_comparison_report(
                self.runs, self.baseline_id, self.candidate_id
            )

        document = Path(result["comparison_html"]).read_text(encoding="utf-8")
        self.assertEqual(result["plot_count"], 1)
        self.assertEqual(result["trace_count"], 2)
        self.assertEqual(result["requirement_regressions"], 1)
        self.assertIn("Added points</span><strong>1", document)
        self.assertIn('<span class="badge added">added</span>', document)
        self.assertIn("&gt;= -3 dB", document)
        self.assertIn("Baseline: R=1k", document)
        self.assertIn("Candidate: R=1k", document)
        self.assertIn('class="badge regression"', document)
        self.assertIn('<svg class="plot"', document)
        self.assertIn("../../mcp-experiment-", document)
        self.assertIn(f"../../{self.baseline_id}/report.html", document)
        self.assertIn(f"../../{self.candidate_id}/report.html", document)
        self.assertIn("gain &lt;script&gt;alert(1)&lt;/script&gt;", document)
        self.assertNotIn("<script>alert(1)</script>", document)
        first = Path(result["comparison_html"]).read_bytes()

        with patch.object(
            experiment_visualization.experiment_report.raw_parser,
            "parse_raw",
            side_effect=[self._raw_data(), self._raw_data(0.8)],
        ):
            repeated = experiment_visualization.build_comparison_report(
                self.runs, self.baseline_id, self.candidate_id
            )
        self.assertEqual(Path(repeated["comparison_html"]).read_bytes(), first)

    def test_comparison_supports_portable_windows_raw_references(self) -> None:
        results_path = self.runs / self.candidate_id / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        analysis = results["points"][0]["analyses"][0]["analysis"]
        analysis["raw_file"] = (
            rf"C:\copied\runs\{self.candidate_id}\native-batch\circuit.raw"
        )
        results_path.write_text(json.dumps(results), encoding="utf-8")

        with patch.object(
            experiment_visualization.experiment_report.raw_parser,
            "parse_raw",
            side_effect=[self._raw_data(), self._raw_data(0.8)],
        ):
            result = experiment_visualization.build_comparison_report(
                self.runs, self.baseline_id, self.candidate_id
            )

        document = Path(result["comparison_html"]).read_text(encoding="utf-8")
        self.assertIn(
            f"../../{self.candidate_id}/native-batch/circuit.raw", document
        )
        self.assertNotIn("C:%5C", document)

    def test_dashboard_rebuilds_index_and_isolates_bad_comparisons(self) -> None:
        comparison_dir = self.runs / "comparisons" / "comparison-0123456789abcdef"
        comparison_dir.mkdir(parents=True)
        (comparison_dir / "comparison.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "comparison_id": "0123456789abcdef",
                    "baseline_experiment_id": self.baseline_id,
                    "candidate_experiment_id": self.candidate_id,
                    "requirement_regressions": 1,
                    "requirement_improvements": 0,
                    "points": [],
                }
            ),
            encoding="utf-8",
        )
        (self.runs / self.baseline_id / "report.html").write_text(
            "report", encoding="utf-8"
        )

        result = experiment_visualization.build_experiment_dashboard(self.runs)

        document = Path(result["dashboard_html"]).read_text(encoding="utf-8")
        self.assertEqual(result["experiment_count"], 2)
        self.assertEqual(result["comparison_count"], 0)
        self.assertEqual(result["issue_count"], 1)
        self.assertIn(self.baseline_id, document)
        self.assertIn(self.candidate_id, document)
        self.assertIn("Search IDs, status, mode, or parameters", document)
        self.assertIn('id="status"', document)
        self.assertIn(f'{self.baseline_id}/report.html', document)
        self.assertTrue((self.runs / "experiments.sqlite3").is_file())

    def test_comparison_report_confines_output_to_runs(self) -> None:
        outside = Path(self.temporary_directory.name) / "outside"
        with (
            patch.object(experiment_visualization, "_comparison_plots", return_value=[]),
            patch.object(
                experiment_visualization.experiment_engine,
                "compare_experiments",
                return_value={"comparison_dir": str(outside)},
            ),
        ):
            with self.assertRaisesRegex(ValueError, "inside the runs directory"):
                experiment_visualization.build_comparison_report(
                    self.runs, self.baseline_id, self.candidate_id
                )
        self.assertFalse(outside.exists())

    def test_report_links_skip_symlinks_outside_runs(self) -> None:
        outside = Path(self.temporary_directory.name) / "outside-report.html"
        outside.write_text("outside", encoding="utf-8")
        report = self.runs / self.baseline_id / "report.html"
        try:
            report.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with patch.object(
            experiment_visualization.experiment_report.raw_parser,
            "parse_raw",
            side_effect=[self._raw_data(), self._raw_data(0.8)],
        ):
            result = experiment_visualization.build_comparison_report(
                self.runs, self.baseline_id, self.candidate_id
            )

        document = Path(result["comparison_html"]).read_text(encoding="utf-8")
        self.assertNotIn("baseline report", document)
        self.assertNotIn(str(outside), document)

    def test_dashboard_links_and_searches_a_valid_comparison(self) -> None:
        comparison = experiment_visualization.experiment_engine.compare_experiments(
            self.runs, self.baseline_id, self.candidate_id
        )
        comparison_dir = Path(comparison["comparison_dir"])
        (comparison_dir / "comparison.html").write_text("report", encoding="utf-8")

        result = experiment_visualization.build_experiment_dashboard(self.runs)

        document = Path(result["dashboard_html"]).read_text(encoding="utf-8")
        comparison_id = comparison["comparison_id"]
        self.assertEqual(result["comparison_count"], 1)
        self.assertIn(
            f'data-search="{comparison_id} {self.baseline_id} {self.candidate_id}"',
            document,
        )
        self.assertIn(
            f"comparisons/comparison-{comparison_id}/comparison.html", document
        )

    def test_atomic_html_replace_preserves_previous_output(self) -> None:
        output = self.runs / "dashboard.html"
        output.write_text("previous", encoding="utf-8")

        with patch.object(
            experiment_visualization.os,
            "replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaisesRegex(OSError, "replace failed"):
                experiment_visualization._write_text(output, "candidate")

        self.assertEqual(output.read_text(encoding="utf-8"), "previous")
        self.assertEqual(list(self.runs.glob(".dashboard.html.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
