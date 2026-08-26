from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import statistical_results
import statistical_engine


class StatisticalResultsTests(unittest.TestCase):
    @staticmethod
    def provenance_source(*, include_method: bool = True) -> dict[str, object]:
        plan_sha256 = "0123456789abcdef" + "0" * 48
        source: dict[str, object] = {
            "kind": "statistical",
            "generator_version": "sha256-stratified-gaussian-v7",
            "plan_id": "statistical-plan-0123456789abcdef",
            "plan_sha256": plan_sha256,
            "definition_hash": "a" * 64,
            "runs_relative_path": (
                "statistical-plans/statistical-plan-0123456789abcdef/"
                "statistical_plan.json"
            ),
        }
        if include_method:
            source["sampling_method"] = "halton"
        return source

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

    def test_sampling_provenance_is_validated_and_exported(self) -> None:
        provenance = statistical_results._sampling_provenance(
            self.provenance_source()
        )
        summary = statistical_results.build_statistics(
            {
                "experiment_id": "mcp-experiment-20260824-120000-000000-a1b2c3d4",
                "point_count": 1,
                "points": [self.point(0, passed=True, value=1.0)],
            },
            sampling_provenance=provenance,
        )

        self.assertEqual(summary["schema_version"], 2)
        self.assertEqual(summary["sampling_provenance"], provenance)
        csv_document = statistical_results._csv_document(summary)
        self.assertIn("provenance,sampling_method,halton", csv_document)
        self.assertIn(
            "provenance,generator_version,sha256-stratified-gaussian-v7",
            csv_document,
        )
        self.assertIn(
            "provenance,plan_id,statistical-plan-0123456789abcdef",
            csv_document,
        )

    def test_legacy_sampling_provenance_defaults_to_independent(self) -> None:
        provenance = statistical_results._sampling_provenance(
            self.provenance_source(include_method=False)
        )

        self.assertEqual(provenance["sampling_method"], "independent")

    def test_sampling_provenance_rejects_mismatched_plan_evidence(self) -> None:
        source = self.provenance_source()
        source["plan_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            statistical_results._sampling_provenance(source)

        source = self.provenance_source()
        source["runs_relative_path"] = "../outside.json"
        with self.assertRaisesRegex(ValueError, "artifact path"):
            statistical_results._sampling_provenance(source)

    def test_verified_plan_rejects_missing_tampered_and_mismatched_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runs = Path(temporary_directory) / "runs"
            plan = statistical_engine.build_statistical_plan(
                [
                    {
                        "name": "R",
                        "distribution": "uniform",
                        "minimum": 900,
                        "maximum": 1100,
                        "nominal": 1000,
                    }
                ],
                2,
                7,
                sampling_method="halton",
            )
            saved = statistical_engine.save_statistical_plan(runs, plan)
            source = {
                "kind": "statistical",
                "sampling_method": "halton",
                "generator_version": plan["generator_version"],
                "plan_id": saved["plan_id"],
                "plan_sha256": saved["plan_sha256"],
                "definition_hash": plan["definition_hash"],
                "runs_relative_path": (
                    f"statistical-plans/{saved['plan_id']}/statistical_plan.json"
                ),
            }

            provenance, loaded = statistical_results._verified_sampling_plan(
                runs, source
            )
            self.assertEqual(provenance["plan_id"], saved["plan_id"])
            self.assertEqual(loaded, plan)

            mismatched = {**source, "definition_hash": "f" * 64}
            with self.assertRaisesRegex(ValueError, "metadata"):
                statistical_results._verified_sampling_plan(runs, mismatched)

            plan_path = Path(saved["plan_file"])
            original = plan_path.read_bytes()
            plan_path.write_bytes(original + b"tampered")
            with self.assertRaisesRegex(ValueError, "content address"):
                statistical_results._verified_sampling_plan(runs, source)

            plan_path.unlink()
            with self.assertRaisesRegex(ValueError, "regular file"):
                statistical_results._verified_sampling_plan(runs, source)

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
