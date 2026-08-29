import json
import tempfile
import unittest
from pathlib import Path

import optimization_engine
import optimization_recipe


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_RECIPE = PROJECT_ROOT / "examples/mixed_signal_daq.ltopt.json"


class OptimizationRecipeTests(unittest.TestCase):
    def test_reference_daq_recipe_matches_phase_4_plan(self):
        recipe = optimization_recipe.load_optimization_recipe(REFERENCE_RECIPE)
        preview = optimization_recipe.preview_optimization_recipe(recipe)

        self.assertTrue(preview["valid"])
        self.assertEqual(preview["plan"]["candidate_count"], 16)
        self.assertEqual(preview["plan"]["corner_count"], 2)
        self.assertEqual(preview["plan"]["point_count"], 32)
        self.assertEqual(preview["execution"]["experiments"], ["ac", "transient"])
        self.assertEqual(preview["execution"]["total_run_count"], 64)

        plan = optimization_engine.build_optimization_plan(
            recipe["parameters"],
            recipe["objectives"],
            recipe["constraints"],
            recipe["fixed_parameters"],
            recipe["corner_axes"],
        )
        plan_id, digest = optimization_engine.optimization_plan_identity(plan)
        self.assertEqual(preview["plan"]["plan_id"], plan_id)
        self.assertEqual(preview["plan"]["plan_sha256"], digest)

    def test_preview_is_non_mutating(self):
        recipe = optimization_recipe.load_optimization_recipe(REFERENCE_RECIPE)
        with tempfile.TemporaryDirectory() as temporary:
            before = list(Path(temporary).iterdir())
            preview = optimization_recipe.preview_optimization_recipe(recipe)
            after = list(Path(temporary).iterdir())
        self.assertTrue(preview["valid"])
        self.assertEqual(before, after)

    def test_publish_requires_the_exact_previewed_workload(self):
        recipe = optimization_recipe.load_optimization_recipe(REFERENCE_RECIPE)
        preview = optimization_recipe.preview_optimization_recipe(recipe)
        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary) / "runs"
            with self.assertRaisesRegex(ValueError, "run count changed"):
                optimization_recipe.publish_optimization_recipe_plan(
                    recipe,
                    runs,
                    preview["recipe"]["sha256"],
                    preview["plan"]["plan_id"],
                    preview["plan"]["point_count"],
                    63,
                )
            _resolved, published = optimization_recipe.publish_optimization_recipe_plan(
                recipe,
                runs,
                preview["recipe"]["sha256"],
                preview["plan"]["plan_id"],
                preview["plan"]["point_count"],
                preview["execution"]["total_run_count"],
            )

        self.assertEqual(published["plan_id"], preview["plan"]["plan_id"])

    def test_domain_edit_recalculates_identity_and_workload(self):
        recipe = optimization_recipe.load_optimization_recipe(REFERENCE_RECIPE)
        reference = optimization_recipe.preview_optimization_recipe(recipe)
        recipe["parameters"][3]["count"] = 3

        preview = optimization_recipe.preview_optimization_recipe(recipe)

        self.assertTrue(preview["valid"])
        self.assertEqual(preview["plan"]["candidate_count"], 24)
        self.assertEqual(preview["plan"]["point_count"], 48)
        self.assertEqual(preview["execution"]["total_run_count"], 96)
        self.assertNotEqual(preview["plan"]["plan_id"], reference["plan"]["plan_id"])

    def test_invalid_or_unknown_fields_fail_closed(self):
        recipe = optimization_recipe.load_optimization_recipe(REFERENCE_RECIPE)
        recipe["parameters"][0]["values"] = [1000]
        preview = optimization_recipe.preview_optimization_recipe(recipe)
        self.assertFalse(preview["valid"])
        self.assertIn("2 to 64", preview["errors"][0]["message"])

        recipe = optimization_recipe.load_optimization_recipe(REFERENCE_RECIPE)
        recipe["surprise"] = True
        preview = optimization_recipe.preview_optimization_recipe(recipe)
        self.assertFalse(preview["valid"])
        self.assertIn("unknown", preview["errors"][0]["message"])

    def test_loader_rejects_nonfinite_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.ltopt.json"
            path.write_text(json.dumps({"value": "ok"})[:-1] + ', "x": NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "finite UTF-8 JSON"):
                optimization_recipe.load_optimization_recipe(path)


if __name__ == "__main__":
    unittest.main()
