from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import artifacts
import remote_execution
import remote_study
import statistical_engine


class RemoteStudyTests(unittest.TestCase):
    def _document(self) -> dict[str, object]:
        recipe: dict[str, object] = {
            "schema_version": 1,
            "kind": "statistical",
            "name": "remote study",
            "plan": {
                "variables": [
                    {
                        "name": "R",
                        "distribution": "uniform",
                        "minimum": 900,
                        "maximum": 1100,
                        "unit": "ohm",
                    }
                ],
                "sample_count": 2,
                "seed": 7,
            },
            "experiments": [
                {
                    "name": "ac",
                    "netlist_path": "circuit.cir",
                    "filename": "circuit.cir",
                    "waveform_analyses": [],
                }
            ],
            "execution": {"max_concurrency": 1, "reuse_cache": False},
        }
        plan = statistical_engine.build_statistical_plan(
            recipe["plan"]["variables"],  # type: ignore[index,arg-type]
            2,
            7,
        )
        plan_artifact = statistical_engine._artifact_bytes(plan)
        plan_sha256 = artifacts.sha256_digest(plan_artifact)
        preview = remote_execution.build_remote_preview(
            repository=remote_execution.DEFAULT_REPOSITORY,
            ref=remote_execution.DEFAULT_REF,
            plan_id=f"statistical-plan-{plan_sha256[:16]}",
            plan_sha256=plan_sha256,
            recipe_sha256=artifacts.definition_hash(recipe),
            plan_artifact=(
                f"runs/statistical-plans/statistical-plan-{plan_sha256[:16]}/"
                "statistical_plan.json"
            ),
            point_count=2,
            experiment_count=1,
            total_run_count=2,
        )
        experiments = [
            {
                "name": "ac",
                "filename": "circuit.cir",
                "netlist_template": "V1 in 0 AC 1\nR1 in 0 {R}\n.ac lin 2 1 2\n.end\n",
                "waveform_analyses": [],
            }
        ]
        envelope = remote_execution.build_remote_envelope(
            preview=preview,
            recipe=recipe,
            plan_artifact=plan_artifact,
            experiments=experiments,
        )
        return remote_execution.decode_remote_envelope(
            envelope["encoded"], envelope["envelope_sha256"]
        )

    def test_remote_document_validates_exact_preview_plan_and_experiments(self) -> None:
        document = self._document()
        self.assertEqual(remote_study.validate_remote_document(document), document)

        changed = {**document, "experiments": [dict(document["experiments"][0])]}  # type: ignore[index]
        changed["experiments"][0]["filename"] = "changed.cir"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "differs from its recipe"):
            remote_study.validate_remote_document(changed)

    def test_evidence_manifest_detects_tampering_and_unlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = {
                "preview_id": "remote-preview-0123456789abcdef",
                "preview_sha256": "1" * 64,
                "github_run_id": "123",
            }
            (root / "remote_execution_summary.json").write_bytes(
                artifacts.canonical_bytes(summary, pretty=True, trailing_newline=True)
            )
            (root / "result.raw").write_bytes(b"raw evidence")
            remote_study.write_remote_evidence_manifest(root, summary)

            verified = remote_study.verify_remote_evidence(
                root,
                expected_preview_id="remote-preview-0123456789abcdef",
                expected_preview_sha256="1" * 64,
                expected_run_id="123",
            )
            self.assertEqual(verified, summary)

            (root / "result.raw").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "integrity failed"):
                remote_study.verify_remote_evidence(
                    root,
                    expected_preview_id="remote-preview-0123456789abcdef",
                    expected_preview_sha256="1" * 64,
                    expected_run_id="123",
                )

            (root / "result.raw").write_bytes(b"raw evidence")
            (root / "unlisted.txt").write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inventory does not match"):
                remote_study.verify_remote_evidence(
                    root,
                    expected_preview_id="remote-preview-0123456789abcdef",
                    expected_preview_sha256="1" * 64,
                    expected_run_id="123",
                )

    def test_remote_summary_uses_engine_pass_status(self) -> None:
        document = self._document()
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            experiment_dir = evidence / "experiment"
            with (
                patch(
                    "remote_study.mcp_server.run_statistical_experiment",
                    return_value={
                        "experiment_id": "mcp-experiment-test",
                        "status": "completed",
                        "point_count": 2,
                        "completed_points": 2,
                        "error_points": 0,
                        "all_passed": True,
                    },
                ),
                patch(
                    "remote_study.statistical_results.summarize_statistical_experiment",
                    return_value={"invalid_points": 0},
                ),
                patch(
                    "remote_study.worst_case_analysis.analyze_statistical_worst_cases",
                    return_value={
                        "worst_cases_json": str(experiment_dir / "worst.json")
                    },
                ),
                patch(
                    "remote_study.sensitivity_analysis.analyze_statistical_sensitivity",
                    return_value={
                        "sensitivity_json": str(experiment_dir / "sensitivity.json")
                    },
                ),
                patch(
                    "remote_study.experiment_report.build_experiment_report",
                    return_value={
                        "report_html": str(experiment_dir / "report.html")
                    },
                ),
            ):
                summary = remote_study.run_remote_study(document, evidence)

            self.assertTrue(summary["experiments"][0]["all_passed"])

class RemoteWorkflowContractTests(unittest.TestCase):
    def test_workflow_accepts_and_verifies_remote_envelope(self) -> None:
        workflow = (
            Path(__file__).parents[1]
            / ".github/workflows/ltspice-windows-real.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("remote_envelope:", workflow)
        self.assertIn("remote_envelope_sha256:", workflow)
        self.assertIn("real_ltspice_remote.py --validate-only", workflow)
        self.assertIn("if: env.REMOTE_MODE == '1'", workflow)
        self.assertIn("remote-ltspice-$env:REMOTE_PREVIEW_ID", workflow)
        self.assertIn("Upload submitted remote-study evidence", workflow)
        self.assertIn("path: ${{ runner.temp }}/ltspice-remote-study", workflow)
        self.assertIn("remote_evidence_manifest.json", Path(__file__).parents[1].joinpath("remote_study.py").read_text(encoding="utf-8"))
