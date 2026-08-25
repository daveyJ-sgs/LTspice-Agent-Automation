from __future__ import annotations

import json
import hashlib
import statistics
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(
            hashlib.sha256(statistical_engine._artifact_bytes(plan)).hexdigest(),
            "d76f4c9d1381ed738526e056144b82ff88c9829a0d20b8057cd24f044231c587",
        )

    def test_mixed_plan_matches_gaussian_and_discrete_golden_values(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "R",
                    "distribution": "uniform",
                    "minimum": 9_000,
                    "maximum": 11_000,
                    "nominal": 10_000,
                    "unit": "ohm",
                },
                {
                    "name": "C",
                    "distribution": "gaussian",
                    "minimum": 8e-7,
                    "maximum": 1.2e-6,
                    "nominal": 1e-6,
                    "sigma": 5e-8,
                    "unit": "F",
                },
                {
                    "name": "GRADE",
                    "distribution": "discrete",
                    "values": ["A", "B", "C"],
                    "weights": [1, 3, 1],
                    "nominal": "B",
                },
            ],
            5,
            20260824,
        )

        self.assertEqual(
            plan["generator_version"], "sha256-counter-distributions-v2"
        )
        self.assertEqual(
            plan["definition_hash"],
            "cb648d9877d1ceb96531336a6222a4f849dd297c71012cf76a20b0d615639b28",
        )
        self.assertEqual(
            plan["definition"]["variables"][2]["weights"],
            ["0.2", "0.6", "0.2"],
        )
        self.assertEqual(
            [point["parameters"] for point in plan["points"]],
            [
                {"R": "10405.863857967846", "C": "0.0000011081569630639257", "GRADE": "B"},
                {"R": "9481.4855010878273", "C": "9.6467201742930863e-7", "GRADE": "A"},
                {"R": "9003.8076717381499", "C": "9.7659836912315381e-7", "GRADE": "B"},
                {"R": "9371.7335773366397", "C": "9.0301480365263195e-7", "GRADE": "A"},
                {"R": "10201.302697503848", "C": "0.0000010441196255102357", "GRADE": "C"},
            ],
        )

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

    def test_mixed_draws_are_stable_per_variable_when_reordered(self) -> None:
        variables = [
            {
                "name": "G",
                "distribution": "gaussian",
                "minimum": -3.0,
                "maximum": 3.0,
                "nominal": 0.0,
                "sigma": 1.0,
            },
            {
                "name": "D",
                "distribution": "discrete",
                "values": ["10k", "11k"],
                "weights": [4, 1],
            },
        ]
        original = statistical_engine.build_statistical_plan(variables, 20, 41)
        reordered = statistical_engine.build_statistical_plan(
            list(reversed(variables)), 20, 41
        )
        for index in range(20):
            for name in ("G", "D"):
                self.assertEqual(
                    original["points"][index]["parameters"][name],
                    reordered["points"][index]["parameters"][name],
                )

    def test_discrete_cumulative_boundaries_choose_the_next_bin(self) -> None:
        variable = {
            "name": "D",
            "values": ["A", "B", "C"],
            "weights": ["1", "2", "1"],
        }
        with patch.object(
            statistical_engine, "_distribution_fraction", return_value=Decimal("0.25")
        ):
            self.assertEqual(statistical_engine._weighted_discrete(variable, 0, 0), "B")
        with patch.object(
            statistical_engine, "_distribution_fraction", return_value=Decimal("0.75")
        ):
            self.assertEqual(statistical_engine._weighted_discrete(variable, 0, 0), "C")

    def test_equivalent_discrete_weights_normalize_to_the_same_plan(self) -> None:
        first = {
            "name": "D",
            "distribution": "discrete",
            "values": ["A", "B", "C"],
            "weights": [1, 3, 1],
        }
        scaled = {**first, "weights": [2, 6, 2]}
        self.assertEqual(
            statistical_engine.build_statistical_plan([first], 20, 7),
            statistical_engine.build_statistical_plan([scaled], 20, 7),
        )

    def test_distribution_population_is_statistically_sane_and_bounded(self) -> None:
        plan = statistical_engine.build_statistical_plan(
            [
                {
                    "name": "G",
                    "distribution": "gaussian",
                    "minimum": -4.0,
                    "maximum": 4.0,
                    "nominal": 0.0,
                    "sigma": 1.0,
                },
                {
                    "name": "D",
                    "distribution": "discrete",
                    "values": ["A", "B", "C"],
                    "weights": [1, 3, 1],
                },
            ],
            1_000,
            7,
        )
        gaussian = [float(point["parameters"]["G"]) for point in plan["points"]]
        discrete = [point["parameters"]["D"] for point in plan["points"]]
        self.assertTrue(all(-4.0 <= value <= 4.0 for value in gaussian))
        self.assertLess(abs(statistics.mean(gaussian)), 0.1)
        self.assertGreater(statistics.pstdev(gaussian), 0.9)
        self.assertLess(statistics.pstdev(gaussian), 1.1)
        self.assertGreater(discrete.count("B"), discrete.count("A") * 2)
        self.assertGreater(discrete.count("B"), discrete.count("C") * 2)

    def test_gaussian_rejection_work_is_bounded(self) -> None:
        with patch.object(
            statistical_engine,
            "_distribution_fraction",
            return_value=Decimal("0.5"),
        ), self.assertRaisesRegex(ValueError, "within 4096 attempts"):
            statistical_engine.build_statistical_plan(
                [
                    {
                        "name": "G",
                        "distribution": "gaussian",
                        "minimum": -1.0,
                        "maximum": 1.0,
                        "nominal": 0.0,
                        "sigma": 1.0,
                    }
                ],
                1,
                0,
            )

    def test_definition_validation_is_fail_closed(self) -> None:
        invalid = [
            ([], 1, 0, "non-empty"),
            (self.variables(), 0, 0, "sample_count"),
            (self.variables(), 1, True, "seed"),
            (self.variables(), 1, -1, "seed"),
            (
                [
                    {
                        "name": "U",
                        "distribution": "uniform",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "sigma": 0.1,
                    }
                ],
                1,
                0,
                "not valid for uniform",
            ),
            (
                [
                    {
                        "name": "R",
                        "distribution": "lognormal",
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
                        "name": "G",
                        "distribution": "gaussian",
                        "minimum": -1.0,
                        "maximum": 1.0,
                        "sigma": 0.2,
                    }
                ],
                1,
                0,
                "nominal is required",
            ),
            (
                [
                    {
                        "name": "G",
                        "distribution": "gaussian",
                        "minimum": -1.0,
                        "maximum": 1.0,
                        "nominal": 0.0,
                        "sigma": 0.0,
                    }
                ],
                1,
                0,
                "positive",
            ),
            (
                [
                    {
                        "name": "G",
                        "distribution": "gaussian",
                        "minimum": -0.01,
                        "maximum": 0.01,
                        "nominal": 0.0,
                        "sigma": 1.0,
                    }
                ],
                1,
                0,
                "span",
            ),
            (
                [
                    {
                        "name": "D",
                        "distribution": "discrete",
                        "values": ["A", "B"],
                        "weights": [1],
                    }
                ],
                1,
                0,
                "weights must match",
            ),
            (
                [
                    {
                        "name": "D",
                        "distribution": "discrete",
                        "values": ["A", "B"],
                        "weights": [1, 0],
                    }
                ],
                1,
                0,
                "positive",
            ),
            (
                [
                    {
                        "name": "D",
                        "distribution": "discrete",
                        "values": ["A", "A"],
                        "weights": [1, 1],
                    }
                ],
                1,
                0,
                "unique",
            ),
            (
                [
                    {
                        "name": "D",
                        "distribution": "discrete",
                        "values": ["A", "B"],
                        "weights": [1, 1],
                        "nominal": "C",
                    }
                ],
                1,
                0,
                "one of",
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
