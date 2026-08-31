from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import artifacts
import github_remote
import remote_execution
import remote_study


class FakeGitHubRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdin = kwargs.get("input")
        self.calls.append((command, stdin if isinstance(stdin, str) else None))
        arguments = command[1:]
        if arguments[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if arguments[:2] == ["workflow", "run"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "https://github.com/daveyJ-sgs/LTspice-Agent-Automation/actions/runs/123\n",
                "",
            )
        if arguments[:2] == ["run", "view"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "status": "completed",
                        "conclusion": "success",
                        "url": "https://github.com/daveyJ-sgs/LTspice-Agent-Automation/actions/runs/123",
                        "displayTitle": "Real LTspice remote-preview-0123456789abcdef",
                        "workflowName": "Real LTspice Windows qualification",
                    }
                ),
                "",
            )
        if arguments[:1] == ["api"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "artifacts": [
                            {
                                "name": (
                                    "remote-ltspice-"
                                    "remote-preview-0123456789abcdef-123"
                                ),
                                "expired": False,
                                "size_in_bytes": 1024,
                            }
                        ]
                    }
                ),
                "",
            )
        if arguments[:2] == ["run", "download"]:
            destination = Path(arguments[arguments.index("--dir") + 1])
            summary = {
                "preview_id": "remote-preview-0123456789abcdef",
                "preview_sha256": "1" * 64,
                "github_run_id": "123",
                "experiments": [
                    {
                        "name": "ac",
                        "report_html": "experiment/report.html",
                    }
                ],
            }
            (destination / "experiment").mkdir()
            (destination / "experiment/report.html").write_text(
                "report", encoding="utf-8"
            )
            (destination / "remote_execution_summary.json").write_bytes(
                artifacts.canonical_bytes(summary, pretty=True, trailing_newline=True)
            )
            remote_study.write_remote_evidence_manifest(destination, summary)
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected gh command: {arguments}")


class GitHubRemoteTests(unittest.TestCase):
    def _preview(self) -> dict[str, object]:
        return {
            "target": {
                "repository": "daveyJ-sgs/LTspice-Agent-Automation",
                "ref": "main",
                "workflow": remote_execution.WORKFLOW_FILE,
            },
            "preview_id": "remote-preview-0123456789abcdef",
            "preview_sha256": "1" * 64,
            "plan": {"plan_id": "statistical-plan-0123456789abcdef"},
            "workload": {"total_run_count": 2},
        }

    def _envelope(self) -> dict[str, str | int]:
        return {
            "schema_version": 1,
            "envelope_id": "remote-envelope-abcdef0123456789",
            "envelope_sha256": "a" * 64,
            "encoded": "study-payload",
            "decoded_bytes": 100,
            "encoded_bytes": 13,
        }

    def test_empty_job_listing_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_dir = Path(temporary) / "runs"
            client = github_remote.GitHubRemote(runs_dir)

            self.assertEqual(client.list_jobs(), [])
            self.assertFalse(runs_dir.exists())

    def test_dispatch_is_idempotent_and_never_persists_payload_or_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "github_remote.shutil.which", return_value="/usr/local/bin/gh"
        ):
            runner = FakeGitHubRunner()
            client = github_remote.GitHubRemote(
                Path(temporary) / "runs", command_runner=runner
            )
            first = client.dispatch(self._preview(), self._envelope())
            second = client.dispatch(self._preview(), self._envelope())

            self.assertEqual(first, second)
            self.assertEqual(first["run_id"], "123")
            dispatches = [call for call in runner.calls if call[0][1:3] == ["workflow", "run"]]
            self.assertEqual(len(dispatches), 1)
            command, stdin = dispatches[0]
            self.assertNotIn("study-payload", command)
            self.assertIn("study-payload", str(stdin))
            record = (
                Path(temporary)
                / "runs/remote-jobs/remote-job-aaaaaaaaaaaaaaaa/remote_job.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn("study-payload", record)
            self.assertNotIn("token", record.lower())

    def test_refresh_and_download_admit_only_verified_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "github_remote.shutil.which", return_value="/usr/local/bin/gh"
        ):
            runner = FakeGitHubRunner()
            client = github_remote.GitHubRemote(
                Path(temporary) / "runs", command_runner=runner
            )
            client.dispatch(self._preview(), self._envelope())
            refreshed = client.refresh("remote-job-aaaaaaaaaaaaaaaa")
            downloaded = client.download("remote-job-aaaaaaaaaaaaaaaa")

            self.assertEqual(refreshed["conclusion"], "success")
            self.assertTrue(downloaded["evidence_available"])
            self.assertEqual(
                downloaded["reports"][0]["url"],
                "/evidence/remote-jobs/remote-job-aaaaaaaaaaaaaaaa/"
                "evidence/experiment/report.html",
            )
            evidence = (
                Path(temporary)
                / "runs/remote-jobs/remote-job-aaaaaaaaaaaaaaaa/evidence"
            )
            self.assertTrue((evidence / "remote_evidence_manifest.json").is_file())
