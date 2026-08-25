from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import statistical_engine


class StatisticalEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary_directory.name) / "runs"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def variables() -> list[statistical_engine.StatisticalVariable]:
        return [
            {
                "name": "R",
                "distribution": "uniform",
                "minimum": 9_000.0,
                "maximum": 11_000.0,
                "nominal": 10_000.0,
                "unit": "ohm",
            },
            {
                "name": "C",
                "distribution": "uniform",
                "minimum": 9e-7,
                "maximum": 1.1e-6,
                "nominal": 1e-6,
                "unit": "F",
            },
        ]

    def test_uniform_plan_matches_golden_values_and_preserves_pairs(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            self.variables(), 3, 20260824
        )

        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["generator_version"], "sha256-counter-uniform-v1")
        self.assertEqual(
            plan["definition_hash"],
            "6f0be87ecee638e66c8d09cd8e7a0c0e896251752a839c3d8e091f1e123c333f",
        )
        self.assertEqual(plan["parameter_order"], ["R", "C"])
        self.assertEqual(
            [point["parameters"] for point in plan["points"]],
            [
                {"R": "10405.863857967846", "C": "0.00000106897441448035"},
                {"R": "9481.4855010878273", "C": "9.9330653105920143e-7"},
                {"R": "9003.8076717381499", "C": "0.0000010261437035727548"},
            ],
        )
        self.assertEqual(len(plan["points"]), 3)

    def test_draws_are_stable_per_variable_and_change_with_seed(self) -> None:
        original = statistical_engine.build_statistical_plan(
            self.variables(), 3, 20260824
        )
        reordered = statistical_engine.build_statistical_plan(
            list(reversed(self.variables())), 3, 20260824
        )
        changed = statistical_engine.build_statistical_plan(
            self.variables(), 3, 20260825
        )

        for index in range(3):
            self.assertEqual(
                original["points"][index]["parameters"],
                {
                    name: reordered["points"][index]["parameters"][name]
                    for name in ("R", "C")
                },
            )
        self.assertNotEqual(original["points"], changed["points"])

    def test_definition_validation_is_fail_closed(self) -> None:
        invalid = [
            ([], 1, 0, "non-empty"),
            (self.variables(), 0, 0, "sample_count"),
            (self.variables(), 1, True, "seed"),
            (self.variables(), 1, -1, "seed"),
            (
                [
                    {
                        "name": "R",
                        "distribution": "gaussian",
                        "minimum": 1.0,
                        "maximum": 2.0,
                    }
                ],
                1,
                0,
                "distribution",
            ),
            (
                [
                    {
                        "name": "R",
                        "distribution": "uniform",
                        "minimum": 2.0,
                        "maximum": 1.0,
                    }
                ],
                1,
                0,
                "less than",
            ),
            (
                [
                    {
                        "name": "R",
                        "distribution": "uniform",
                        "minimum": 1.0,
                        "maximum": 2.0,
                        "nominal": 3.0,
                    }
                ],
                1,
                0,
                "within",
            ),
            (
                [
                    {
                        "name": "bad-name",
                        "distribution": "uniform",
                        "minimum": 1.0,
                        "maximum": 2.0,
                    }
                ],
                1,
                0,
                "names",
            ),
            (
                [self.variables()[0], self.variables()[0]],
                1,
                0,
                "duplicate",
            ),
        ]
        for variables, sample_count, seed, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError, message
            ):
                statistical_engine.build_statistical_plan(
                    variables, sample_count, seed  # type: ignore[arg-type]
                )

    def test_cell_limit_bounds_plan_payload(self) -> None:
        variables = [
            {
                "name": f"P{index}",
                "distribution": "uniform",
                "minimum": 0.0,
                "maximum": 1.0,
            }
            for index in range(statistical_engine.MAX_STATISTICAL_VARIABLES)
        ]
        with self.assertRaisesRegex(ValueError, "values"):
            statistical_engine.build_statistical_plan(variables, 313, 0)

    def test_plan_publication_is_idempotent_and_content_addressed(self) -> None:
        first = statistical_engine.generate_statistical_plan(
            self.runs, self.variables(), 3, 20260824
        )
        second = statistical_engine.generate_statistical_plan(
            self.runs, self.variables(), 3, 20260824
        )

        self.assertEqual(first, second)
        self.assertRegex(first["plan_id"], r"^statistical-plan-[0-9a-f]{16}$")
        plan_file = Path(first["plan_file"])
        self.assertTrue(plan_file.is_file())
        loaded = statistical_engine.load_statistical_plan(
            self.runs, first["plan_id"]
        )
        self.assertEqual(loaded["points"], first["points"])
        self.assertEqual(json.loads(plan_file.read_text()), loaded)

    def test_load_rejects_tampering_and_symlinks(self) -> None:
        result = statistical_engine.generate_statistical_plan(
            self.runs, self.variables(), 2, 4
        )
        plan_file = Path(result["plan_file"])
        plan_file.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "content address"):
            statistical_engine.load_statistical_plan(self.runs, result["plan_id"])

        other_runs = Path(self.temporary_directory.name) / "other-runs"
        other_runs.mkdir()
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        (other_runs / "statistical-plans").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            statistical_engine.generate_statistical_plan(
                other_runs, self.variables(), 1, 0
            )


if __name__ == "__main__":
    unittest.main()
