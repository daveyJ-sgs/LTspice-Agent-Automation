from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import statistical_results


class StatisticalResultsTests(unittest.TestCase):
    @staticmethod
    def point(
        index: int,
        *,
        passed: bool,
        simulation_status: str = "completed",
        analysis_status: str = "completed",
        value: float = 0.0,
    ) -> dict[str, object]:
        requirement_passed = value <= 3.0
        return {
            "index": index,
            "parameters": {"R": f"{index + 1}k"},
            "run_dir": f"point-{index:04d}",
            "simulation_status": simulation_status,
            "measurements": {"gain": value},
            "analyses": [
                {
                    "name": "response",
                    "status": analysis_status,
                    "analysis": None
                    if analysis_status == "error"
                    else {
                        "results": [
                            {
                                "metric": "gain",
                                "value": value,
                                "unit": "dB",
                                "passed": requirement_passed,
                                "parameters": {},
                                "threshold": {
                                    "operator": "<=",
                                    "target": 3.0,
                                    "unit": "dB",
                                },
                            }
                        ]
                    },
                }
            ],
            "all_passed": passed,
            "error": None,
        }

    def test_hand_calculated_yield_statistics_and_error_accounting(self) -> None:
        points = [
            self.point(0, passed=True, value=1.0),
            self.point(1, passed=True, value=2.0),
            self.point(2, passed=True, value=3.0),
            self.point(3, passed=False, value=4.0),
            self.point(4, passed=False, simulation_status="error", value=100.0),
            self.point(5, passed=False, analysis_status="error", value=200.0),
            self.point(6, passed=False, simulation_status="cancelled", value=300.0),
        ]
        summary = statistical_results.build_statistics(
            {
                "experiment_id": "mcp-experiment-20260824-120000-000000-a1b2c3d4",
                "point_count": 7,
                "points": points,
            }
        )

        self.assertEqual(
            summary["classifications"],
            {
                "electrical_pass": 3,
                "electrical_failure": 1,
                "simulation_error": 1,
                "analysis_error": 1,
                "cancelled": 1,
                "unfinished": 0,
            },
        )
        self.assertEqual(summary["evaluated_points"], 4)
        self.assertEqual(summary["invalid_points"], 3)
        self.assertEqual(summary["observed_yield"], 0.75)
        self.assertAlmostEqual(summary["planned_pass_fraction"], 3 / 7)
        interval = summary["yield_confidence_interval"]
        self.assertAlmostEqual(interval["low"], 0.30064184258240184)
        self.assertAlmostEqual(interval["high"], 0.9544127391902995)
        measurement = summary["measurements"]["gain"]
        self.assertEqual(measurement["count"], 4)
        self.assertEqual(measurement["mean"], 2.5)
        self.assertAlmostEqual(measurement["standard_deviation"], 1.2909944487358056)
        self.assertAlmostEqual(measurement["p05"], 1.15)
        self.assertEqual(measurement["p50"], 2.5)
        self.assertAlmostEqual(measurement["p95"], 3.85)
        margin = summary["requirement_margins"][0]["statistics"]
        self.assertEqual(margin["minimum"], -1.0)
        self.assertEqual(margin["maximum"], 2.0)
        self.assertEqual(summary["failed_samples"][0]["index"], 3)
        self.assertEqual(
            summary["failed_samples"][0]["failed_requirements"][0]["metric"],
            "gain",
        )
        self.assertEqual(summary["measurements"]["gain"]["point_indexes"], [0, 1, 2, 3])
        csv_document = statistical_results._csv_document(summary)
        self.assertIn("yield,observed_yield,0.75", csv_document)
        self.assertIn("classification,electrical_failure,,1", csv_document)
        self.assertIn("measurement,gain", csv_document)
        self.assertIn("requirement_margin,response:gain", csv_document)

    def test_zero_evaluated_points_has_no_yield_or_interval(self) -> None:
        summary = statistical_results.build_statistics(
            {
                "experiment_id": "mcp-experiment-20260824-120000-000000-a1b2c3d4",
                "point_count": 1,
                "points": [
                    self.point(0, passed=False, simulation_status="error", value=1.0)
                ],
            }
        )
        self.assertIsNone(summary["observed_yield"])
        self.assertEqual(
            summary["yield_confidence_interval"],
            {"method": "wilson", "low": None, "high": None},
        )

    def test_named_corners_are_reported_separately_without_implicit_pooling(
        self,
    ) -> None:
        points = [
            self.point(0, passed=True, value=1.0),
            self.point(1, passed=False, value=4.0),
            self.point(2, passed=True, value=2.0),
        ]
        metadata = [
            {"index": 0, "sample_index": 0, "corners": {"temperature": "cold"}},
            {"index": 1, "sample_index": 0, "corners": {"temperature": "hot"}},
            {"index": 2, "sample_index": 1, "corners": {"temperature": "cold"}},
            {"index": 3, "sample_index": 1, "corners": {"temperature": "hot"}},
        ]
        results = {
            "experiment_id": "mcp-experiment-20260824-120000-000000-a1b2c3d4",
            "point_count": 4,
            "points": points,
        }
        summary = statistical_results.build_statistics(
            results,
            point_metadata=metadata,
            corner_aggregate=False,
        )

        self.assertIsNone(summary["observed_yield"])
        self.assertIsNone(summary["planned_pass_fraction"])
        self.assertIsNone(summary["corner_aggregate"])
        self.assertEqual(
            [entry["corners"] for entry in summary["corner_results"]],
            [{"temperature": "cold"}, {"temperature": "hot"}],
        )
        cold, hot = summary["corner_results"]
        self.assertEqual(cold["observed_yield"], 1.0)
        self.assertEqual(cold["evaluated_points"], 2)
        self.assertEqual(cold["invalid_points"], 0)
        self.assertEqual(hot["observed_yield"], 0.0)
        self.assertEqual(hot["evaluated_points"], 1)
        self.assertEqual(hot["invalid_points"], 1)
        self.assertEqual(summary["samples"][0]["sample_index"], 0)
        self.assertEqual(
            summary["samples"][1]["corners"], {"temperature": "hot"}
        )
        self.assertIn("corner_yield", statistical_results._csv_document(summary))

        pooled = statistical_results.build_statistics(
            results,
            point_metadata=metadata,
            corner_aggregate=True,
        )
        self.assertEqual(pooled["corner_aggregate"], "pooled")
        self.assertAlmostEqual(pooled["observed_yield"], 2 / 3)

    def test_statistics_writer_rejects_a_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outside = root / "outside.json"
            outside.write_text("preserve", encoding="utf-8")
            target = root / "statistics.json"
            target.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                statistical_results._write_atomic(target, "changed")
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
