from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import experiment_report
import experiment_index
from raw_parser import RawData


class ExperimentReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary_directory.name) / "runs"
        self.runs.mkdir()
        self.experiment_id = "mcp-experiment-20260824-180000-000000-a1b2c3d4"
        self.experiment_dir = self.runs / self.experiment_id
        self.batch_dir = self.experiment_dir / "native-batch"
        self.batch_dir.mkdir(parents=True)
        self.raw_path = self.batch_dir / "circuit.raw"
        self.raw_path.write_bytes(b"raw-placeholder")
        self._write_artifacts()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_display_sampling_preserves_endpoints_and_narrow_extrema(self) -> None:
        values = [0.0] * 10_000
        values[1] = 5.0
        values[123] = -7.0
        values[5000] = 9.0
        values[9998] = -11.0

        indices = experiment_report._sample_indices(values)

        self.assertLessEqual(len(indices), experiment_report.DISPLAY_POINT_LIMIT)
        self.assertTrue({0, 1, 123, 5000, 9998, 9999}.issubset(indices))

    def test_analysis_panels_escape_content_and_enforce_row_budget(self) -> None:
        statistics = {
            "measurements": {
                "gain<script>": {
                    "count": 2,
                    "minimum": 1.0,
                    "p05": 1.0,
                    "p50": 1.5,
                    "p95": 2.0,
                    "maximum": 2.0,
                    "mean": 1.5,
                    "standard_deviation": 0.5,
                }
            },
            "requirement_margins": [],
        }
        worst_cases = {
            "requirements": [
                {
                    "analysis": "ac<script>",
                    "metric": "gain",
                    "unit": "dB",
                    "worst_cases": [
                        {
                            "evidence_path": "point-0001/",
                            "margin": -0.1,
                            "passed": False,
                            "point_index": 1,
                        }
                    ],
                }
            ]
        }
        sensitivity = {
            "requirements": [
                {
                    "analysis": "ac",
                    "metric": "gain",
                    "scopes": [
                        {
                            "corners": {},
                            "variables": [
                                {
                                    "rank": None,
                                    "variable": "R<script>",
                                    "rho": None,
                                    "status": "constant_input",
                                    "correlated_with": [],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        tornado = {
            "requirements": [
                {
                    "analysis": "ac",
                    "metric": "gain",
                    "unit": "dB",
                    "effects": [
                        {
                            "rank": None,
                            "name": "R<script>",
                            "status": "incomplete",
                            "low_effect": None,
                            "high_effect": None,
                            "impact": None,
                        }
                    ],
                }
            ]
        }

        document = "".join(
            (
                experiment_report._distribution_panel(statistics),
                experiment_report._worst_case_panel(worst_cases),
                experiment_report._sensitivity_panel(sensitivity),
                experiment_report._tornado_panel(tornado),
            )
        )

        self.assertIn("Distribution summaries", document)
        self.assertIn("Worst evidenced cases", document)
        self.assertIn("Global rank sensitivity", document)
        self.assertIn("Local OAT tornado data", document)
        self.assertIn("gain&lt;script&gt;", document)
        self.assertNotIn("gain<script>", document)
        self.assertNotIn(">None<", document)
        with (
            patch.object(experiment_report, "MAX_ANALYSIS_ROWS", 0),
            self.assertRaisesRegex(ValueError, "report budget"),
        ):
            experiment_report._distribution_panel(statistics)
        with (
            patch.object(experiment_report, "MAX_ANALYSIS_ROWS", 0),
            self.assertRaisesRegex(ValueError, "report budget"),
        ):
            experiment_report._sensitivity_panel(sensitivity)

    def _analysis(self, step_index: int, value: float) -> dict[str, object]:
        return {
            "name": "gain </script><script>alert(1)</script>",
            "status": "completed",
            "error": None,
            "analysis": {
                "all_passed": True,
                "analysis_resolution": "full",
                "axis_variable": "frequency",
                "raw_file": str(self.raw_path),
                "results": [
                    {
                        "metric": "ac_gain_db",
                        "value": value,
                        "unit": "dB",
                        "threshold": {
                            "operator": ">=",
                            "target": -3.0,
                            "unit": "dB",
                        },
                        "passed": True,
                        "parameters": {"frequency_value": 1000.0},
                        "evidence": {"frequency": 1000.0},
                    }
                ],
                "secondary_variable": "V(in)",
                "source_points": 4,
                "step_index": step_index,
                "variable": "V(out)",
            },
        }

    def _write_artifacts(self) -> None:
        parameters = [
            {"name": "R", "values": ["1k", "2k"], "unit": "ohm"},
        ]
        definition = {
            "netlist_template": "R1 in out {R}\n.end\n",
            "parameters": parameters,
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
            "experiment_id": self.experiment_id,
            "status": "completed",
            "definition": definition,
            "point_count": 2,
            "finished_points": 2,
            "completed_points": 2,
            "error_points": 0,
            "passed_points": 2,
            "failed_points": 0,
            "all_passed": True,
            "created_at": "2026-08-24T18:00:00-07:00",
            "updated_at": "2026-08-24T18:00:01-07:00",
            "definition_hash": experiment_index._definition_hash(definition),
        }
        points = []
        for index, resistance in enumerate(("1k", "2k")):
            points.append(
                {
                    "index": index,
                    "parameters": {"R": resistance},
                    "run_dir": str(self.batch_dir),
                    "simulation_status": "completed",
                    "duration_seconds": None,
                    "measurements": {"gain": -0.5 - index},
                    "analyses": [self._analysis(index, -0.5 - index)],
                    "all_passed": True,
                    "error": None,
                    "native_step_index": index,
                }
            )
        results = {
            "experiment_id": self.experiment_id,
            "status": "completed",
            "execution_mode": "native",
            "parameter_order": ["R"],
            "derived_parameter_order": [],
            "parameter_units": {"R": "ohm"},
            "point_count": 2,
            "completed_points": 2,
            "error_points": 0,
            "passed_points": 2,
            "failed_points": 0,
            "all_passed": True,
            "points": points,
            "native_batch": None,
        }
        (self.experiment_dir / "experiment_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.experiment_dir / "results.json").write_text(
            json.dumps(results), encoding="utf-8"
        )
        (self.experiment_dir / "results.csv").write_text("index,gain\n", encoding="utf-8")

    @staticmethod
    def _raw_data() -> RawData:
        frequency = [100.0, 1000.0, 10000.0, 100000.0] * 2
        return RawData(
            flags="complex stepped",
            variables=["frequency", "V(out)", "V(in)"],
            values={
                "frequency": [complex(value, 0.0) for value in frequency],
                "V(out)": [
                    complex(value, 0.0)
                    for value in (1.0, 0.9, 0.5, 0.1, 1.0, 0.8, 0.4, 0.05)
                ],
                "V(in)": [complex(1.0, 0.0)] * 8,
            },
            step_count=2,
            points_per_step=4,
        )

    def test_builds_self_contained_report_with_visible_downsampled_overlays(self) -> None:
        with (
            patch.object(experiment_report.raw_parser, "parse_raw", return_value=self._raw_data()),
            patch.object(experiment_report, "DISPLAY_POINT_LIMIT", 3),
        ):
            result = experiment_report.build_experiment_report(
                self.runs, self.experiment_id
            )

        report_path = Path(result["report_html"])
        document = report_path.read_text(encoding="utf-8")
        self.assertEqual(result["plot_count"], 1)
        self.assertEqual(result["trace_count"], 2)
        self.assertEqual(result["source_points"], 8)
        self.assertEqual(result["displayed_points"], 6)
        self.assertIn('<svg class="plot"', document)
        self.assertIn('<path class="trace"', document)
        self.assertIn('class="grid-line"', document)
        self.assertIn("pointermove", document)
        self.assertIn(
            "gain &lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;", document
        )
        self.assertNotIn("</script><script>alert(1)</script>", document)
        self.assertNotIn("https://", document)
        self.assertNotIn("http://", document)
        self.assertIn('href="native-batch/circuit.raw"', document)
        self.assertIn('href="results.json"', document)
        self.assertIn("Full-resolution source: 4 points", document)
        payload_text = document.split(
            '<script id="report-data" type="application/json">', 1
        )[1].split("</script>", 1)[0]
        payload = json.loads(payload_text)
        self.assertEqual(payload[0]["traces"][0]["x"], [100.0, 10000.0, 100000.0])
        self.assertAlmostEqual(payload[0]["traces"][0]["y"][1], -6.0205999)
        first = report_path.read_bytes()
        with patch.object(
            experiment_report.raw_parser, "parse_raw", return_value=self._raw_data()
        ), patch.object(experiment_report, "DISPLAY_POINT_LIMIT", 3):
            repeated = experiment_report.build_experiment_report(
                self.runs, self.experiment_id
            )
        self.assertEqual(Path(repeated["report_html"]).read_bytes(), first)

    def test_resolves_portable_windows_artifact_paths(self) -> None:
        results_path = self.experiment_dir / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        for point in results["points"]:
            point["run_dir"] = rf"C:\copied\runs\{self.experiment_id}\native-batch"
            point["analyses"][0]["analysis"]["raw_file"] = (
                rf"C:\copied\runs\{self.experiment_id}\native-batch\circuit.raw"
            )
        results_path.write_text(json.dumps(results), encoding="utf-8")

        with patch.object(
            experiment_report.raw_parser, "parse_raw", return_value=self._raw_data()
        ):
            result = experiment_report.build_experiment_report(
                self.runs, self.experiment_id
            )

        self.assertEqual(result["plot_count"], 1)
        self.assertIn(
            'href="native-batch/circuit.raw"',
            Path(result["report_html"]).read_text(encoding="utf-8"),
        )

    def test_supports_schema_v1_independent_waveforms(self) -> None:
        manifest_path = self.experiment_dir / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        manifest["started_at"] = manifest.pop("created_at")
        manifest.pop("updated_at")
        manifest.pop("engine_version")
        manifest.pop("finished_points")
        manifest["definition"]["execution_mode"] = "independent"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        results_path = self.experiment_dir / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["execution_mode"] = "independent"
        raw_data = self._raw_data()
        parsed = []
        for point in results["points"]:
            index = point["index"]
            point_dir = self.experiment_dir / f"point-{index:04d}"
            point_dir.mkdir()
            raw_path = point_dir / "circuit.raw"
            raw_path.write_bytes(b"raw-placeholder")
            point["run_dir"] = str(point_dir)
            point.pop("native_step_index")
            analysis = point["analyses"][0]["analysis"]
            analysis["raw_file"] = str(raw_path)
            analysis["step_index"] = 0
            selected = slice(index * 4, (index + 1) * 4)
            parsed.append(
                RawData(
                    flags="complex",
                    variables=raw_data.variables,
                    values={
                        name: values[selected] for name, values in raw_data.values.items()
                    },
                )
            )
        results_path.write_text(json.dumps(results), encoding="utf-8")

        with patch.object(
            experiment_report.raw_parser, "parse_raw", side_effect=parsed
        ):
            report = experiment_report.build_experiment_report(
                self.runs, self.experiment_id
            )

        self.assertEqual(report["trace_count"], 2)
        self.assertTrue(Path(report["report_html"]).is_file())

    def test_corrupt_raw_and_display_budget_fail_before_output(self) -> None:
        with patch.object(
            experiment_report.raw_parser,
            "parse_raw",
            side_effect=ValueError("corrupt RAW"),
        ), self.assertRaisesRegex(ValueError, "corrupt RAW"):
            experiment_report.build_experiment_report(self.runs, self.experiment_id)
        self.assertFalse((self.experiment_dir / "report.html").exists())

        with (
            patch.object(experiment_report.raw_parser, "parse_raw", return_value=self._raw_data()),
            patch.object(experiment_report, "MAX_TRACE_COUNT", 1),
            self.assertRaisesRegex(ValueError, "display budget"),
        ):
            experiment_report.build_experiment_report(self.runs, self.experiment_id)
        self.assertFalse((self.experiment_dir / "report.html").exists())

    def test_rejects_waveform_step_mapped_to_the_wrong_point(self) -> None:
        results_path = self.experiment_dir / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["points"][0]["analyses"][0]["analysis"]["step_index"] = 1
        results_path.write_text(json.dumps(results), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "step does not match"):
            experiment_report.build_experiment_report(self.runs, self.experiment_id)
        self.assertFalse((self.experiment_dir / "report.html").exists())

    def test_rejects_raw_artifact_that_changed_after_analysis(self) -> None:
        results_path = self.experiment_dir / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(self.raw_path.read_bytes()).hexdigest()
        for point in results["points"]:
            analysis = point["analyses"][0]["analysis"]
            analysis["raw_sha256"] = digest
            analysis["raw_size_bytes"] = self.raw_path.stat().st_size
        results_path.write_text(json.dumps(results), encoding="utf-8")
        self.raw_path.write_bytes(b"tampered")

        indexed = experiment_index.build_experiment_index(self.runs)
        self.assertEqual(indexed["result_experiments"], 0)
        self.assertIn("hash does not match", indexed["issues"][0]["message"])
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            experiment_report.build_experiment_report(self.runs, self.experiment_id)
        self.assertFalse((self.experiment_dir / "report.html").exists())

    def test_atomic_replace_failure_preserves_previous_report(self) -> None:
        report_path = self.experiment_dir / "report.html"
        report_path.write_text("previous report", encoding="utf-8")
        with (
            patch.object(experiment_report.raw_parser, "parse_raw", return_value=self._raw_data()),
            patch.object(experiment_report.os, "replace", side_effect=OSError("busy")),
            self.assertRaisesRegex(OSError, "busy"),
        ):
            experiment_report.build_experiment_report(self.runs, self.experiment_id)

        self.assertEqual(report_path.read_text(encoding="utf-8"), "previous report")
        self.assertEqual(list(self.experiment_dir.glob(".report.html.*.tmp")), [])

    def test_rejects_invalid_or_escaped_artifacts_before_output(self) -> None:
        results_path = self.experiment_dir / "results.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["experiment_id"] = "wrong"
        results_path.write_text(json.dumps(results), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "experiment_id"):
            experiment_report.build_experiment_report(self.runs, self.experiment_id)
        self.assertFalse((self.experiment_dir / "report.html").exists())

        self._write_artifacts()
        outside = self.runs.parent / "outside.raw"
        outside.write_bytes(b"outside")
        self.raw_path.unlink()
        try:
            os.symlink(outside, self.raw_path)
        except OSError as exc:
            self.skipTest(f"file symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "inside the experiment directory"):
            experiment_report.build_experiment_report(self.runs, self.experiment_id)
        self.assertFalse((self.experiment_dir / "report.html").exists())


if __name__ == "__main__":
    unittest.main()
