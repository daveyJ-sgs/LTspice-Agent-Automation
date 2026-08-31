"""GitHub CLI-backed dispatch and recovery for System Builder remote studies."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import artifacts
import remote_study
from remote_execution import WORKFLOW_FILE

REMOTE_JOB_SCHEMA_VERSION = 1
REMOTE_JOB_PATTERN = re.compile(r"remote-job-[0-9a-f]{16}")
RUN_URL_PATTERN = re.compile(
    r"https://github\.com/(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/"
    r"actions/runs/(?P<run_id>[0-9]+)"
)
MAX_COMMAND_OUTPUT = 2 * 1024 * 1024
MAX_REMOTE_ARTIFACT_BYTES = 600 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class GitHubRemote:
    """Use an existing GitHub CLI login without handling its credential."""

    def __init__(
        self,
        runs_dir: Path,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.runs_dir = runs_dir
        self.command_runner = command_runner or subprocess.run

    def _root(self) -> Path:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        if self.runs_dir.is_symlink():
            raise ValueError("runs directory must not be a symlink")
        root = self.runs_dir.resolve()
        jobs = root / "remote-jobs"
        if jobs.is_symlink():
            raise ValueError("remote jobs directory must not be a symlink")
        jobs.mkdir(exist_ok=True)
        resolved = jobs.resolve()
        if resolved.parent != root:
            raise ValueError("remote jobs directory must remain inside runs")
        return resolved

    def _job_dir(self, remote_job_id: str, *, create: bool = False) -> Path:
        if not REMOTE_JOB_PATTERN.fullmatch(remote_job_id):
            raise ValueError("invalid remote job identity")
        root = self._root()
        job_dir = root / remote_job_id
        if create:
            job_dir.mkdir(exist_ok=True)
        if job_dir.is_symlink() or not job_dir.is_dir():
            raise FileNotFoundError("remote job was not found")
        resolved = job_dir.resolve()
        if resolved.parent != root or resolved.name != remote_job_id:
            raise ValueError("remote job must remain inside runs")
        return resolved

    def _run(self, arguments: list[str], *, stdin: str | None = None) -> str:
        executable = shutil.which("gh")
        if executable is None:
            raise RuntimeError(
                "GitHub CLI is required; install gh and run `gh auth login` first"
            )
        environment = os.environ.copy()
        environment.update(
            {"GH_PROMPT_DISABLED": "1", "GH_PAGER": "cat", "NO_COLOR": "1"}
        )
        try:
            completed = self.command_runner(
                [executable, *arguments],
                input=stdin,
                text=True,
                capture_output=True,
                timeout=45,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("GitHub CLI request did not complete") from exc
        stdout = completed.stdout[:MAX_COMMAND_OUTPUT]
        stderr = completed.stderr[:MAX_COMMAND_OUTPUT]
        if completed.returncode != 0:
            message = stderr.strip().splitlines()[-1:] or ["GitHub CLI request failed"]
            raise RuntimeError(message[0][:500])
        return stdout

    def auth_status(self) -> dict[str, object]:
        self._run(["auth", "status", "--hostname", "github.com"])
        return {
            "available": True,
            "provider": "github_cli",
            "credential_storage": "managed_by_github_cli",
        }

    def _write_record(self, job_dir: Path, record: dict[str, object]) -> None:
        path = job_dir / "remote_job.json"
        if path.is_symlink():
            raise ValueError("remote job record must not be a symlink")
        content = artifacts.canonical_bytes(record, pretty=True, trailing_newline=True)
        temporary = job_dir / f".remote_job.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_record(self, remote_job_id: str) -> dict[str, object]:
        path = self._job_dir(remote_job_id) / "remote_job.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            raise ValueError("remote job record is invalid")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("remote job record is invalid") from exc
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != REMOTE_JOB_SCHEMA_VERSION
            or record.get("remote_job_id") != remote_job_id
        ):
            raise ValueError("remote job record identity does not match")
        return record

    def list_jobs(self) -> list[dict[str, object]]:
        if not self.runs_dir.exists():
            return []
        if self.runs_dir.is_symlink() or not self.runs_dir.is_dir():
            raise ValueError("runs directory must be a regular directory")
        root = self.runs_dir.resolve()
        remote_root = root / "remote-jobs"
        if not remote_root.exists():
            return []
        if remote_root.is_symlink() or not remote_root.is_dir():
            raise ValueError("remote jobs directory must be a regular directory")
        remote_root = remote_root.resolve()
        if remote_root.parent != root:
            raise ValueError("remote jobs directory must remain inside runs")
        jobs: list[dict[str, object]] = []
        for path in sorted(remote_root.glob("remote-job-*"), reverse=True):
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                jobs.append(self._load_record(path.name))
            except (OSError, ValueError):
                continue
        return jobs[:32]

    @staticmethod
    def _run_identity(url: str, repository: str) -> tuple[str, str]:
        match = RUN_URL_PATTERN.fullmatch(url.strip())
        if match is None or match.group("repository") != repository:
            raise RuntimeError("GitHub dispatch did not return the expected run URL")
        return match.group("run_id"), url.strip()

    def _recover_run(self, record: dict[str, object]) -> tuple[str, str] | None:
        repository = str(record["repository"])
        preview_id = str(record["preview_id"])
        output = self._run(
            [
                "run",
                "list",
                "--repo",
                repository,
                "--workflow",
                WORKFLOW_FILE,
                "--event",
                "workflow_dispatch",
                "--limit",
                "20",
                "--json",
                "databaseId,displayTitle,url",
            ]
        )
        try:
            runs = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub run recovery returned invalid JSON") from exc
        if not isinstance(runs, list):
            raise RuntimeError("GitHub run recovery returned invalid data")
        title = f"Real LTspice {preview_id}"
        matches = [
            run
            for run in runs
            if isinstance(run, dict) and run.get("displayTitle") == title
        ]
        if len(matches) > 1:
            raise RuntimeError("multiple GitHub runs match this remote preview")
        if not matches:
            return None
        return self._run_identity(str(matches[0].get("url", "")), repository)

    def dispatch(
        self,
        preview: dict[str, object],
        envelope: dict[str, str | int],
    ) -> dict[str, object]:
        target = preview["target"]
        assert isinstance(target, dict)
        repository = str(target["repository"])
        remote_job_id = f"remote-job-{str(envelope['envelope_sha256'])[:16]}"
        job_dir = self._job_dir(remote_job_id, create=True)
        record_path = job_dir / "remote_job.json"
        created = not record_path.exists()
        if created:
            record: dict[str, object] = {
                "schema_version": REMOTE_JOB_SCHEMA_VERSION,
                "remote_job_id": remote_job_id,
                "state": "dispatching",
                "repository": repository,
                "ref": target["ref"],
                "workflow": target["workflow"],
                "preview_id": preview["preview_id"],
                "preview_sha256": preview["preview_sha256"],
                "envelope_id": envelope["envelope_id"],
                "envelope_sha256": envelope["envelope_sha256"],
                "plan": preview["plan"],
                "workload": preview["workload"],
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "run_id": None,
                "run_url": None,
                "status": "dispatching",
                "conclusion": None,
                "evidence_available": False,
                "reports": [],
            }
            self._write_record(job_dir, record)
        else:
            record = self._load_record(remote_job_id)
            if (
                record.get("preview_sha256") != preview["preview_sha256"]
                or record.get("envelope_sha256") != envelope["envelope_sha256"]
            ):
                raise ValueError("existing remote job identity differs")
            if record.get("run_id") is not None:
                return record
            recovered = self._recover_run(record)
            if recovered is None:
                raise RuntimeError(
                    "dispatch outcome is unresolved; refresh after GitHub lists the run"
                )
            run_id, run_url = recovered
            record.update(
                {
                    "state": "submitted",
                    "status": "queued",
                    "run_id": run_id,
                    "run_url": run_url,
                    "updated_at": _utc_now(),
                }
            )
            self._write_record(job_dir, record)
            return record

        inputs = {
            "remote_preview_id": preview["preview_id"],
            "remote_envelope_sha256": envelope["envelope_sha256"],
            "remote_envelope": envelope["encoded"],
        }
        try:
            output = self._run(
                [
                    "workflow",
                    "run",
                    WORKFLOW_FILE,
                    "--repo",
                    repository,
                    "--ref",
                    str(target["ref"]),
                    "--json",
                ],
                stdin=json.dumps(inputs, separators=(",", ":")),
            )
            run_id, run_url = self._run_identity(output.strip(), repository)
        except RuntimeError as exc:
            record.update(
                {
                    "state": "dispatch_unknown",
                    "status": "unknown",
                    "error": str(exc),
                    "updated_at": _utc_now(),
                }
            )
            self._write_record(job_dir, record)
            raise
        record.update(
            {
                "state": "submitted",
                "status": "queued",
                "run_id": run_id,
                "run_url": run_url,
                "updated_at": _utc_now(),
            }
        )
        self._write_record(job_dir, record)
        return record

    def refresh(self, remote_job_id: str) -> dict[str, object]:
        record = self._load_record(remote_job_id)
        if record.get("run_id") is None:
            recovered = self._recover_run(record)
            if recovered is None:
                return record
            record["run_id"], record["run_url"] = recovered
        output = self._run(
            [
                "run",
                "view",
                str(record["run_id"]),
                "--repo",
                str(record["repository"]),
                "--json",
                "status,conclusion,url,displayTitle,workflowName",
            ]
        )
        try:
            remote = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub run status returned invalid JSON") from exc
        if (
            not isinstance(remote, dict)
            or remote.get("displayTitle") != f"Real LTspice {record['preview_id']}"
            or remote.get("workflowName") != "Real LTspice Windows qualification"
        ):
            raise RuntimeError("GitHub run identity does not match the remote job")
        record.update(
            {
                "state": "completed"
                if remote.get("status") == "completed"
                else "running",
                "status": remote.get("status"),
                "conclusion": remote.get("conclusion"),
                "run_url": remote.get("url"),
                "updated_at": _utc_now(),
            }
        )
        self._write_record(self._job_dir(remote_job_id), record)
        return record

    def download(self, remote_job_id: str) -> dict[str, object]:
        record = self.refresh(remote_job_id)
        if record.get("status") != "completed" or record.get("conclusion") != "success":
            raise RuntimeError("remote evidence is available only after a successful run")
        job_dir = self._job_dir(remote_job_id)
        final = job_dir / "evidence"
        if final.is_dir() and not final.is_symlink():
            summary = remote_study.verify_remote_evidence(
                final,
                expected_preview_id=str(record["preview_id"]),
                expected_preview_sha256=str(record["preview_sha256"]),
                expected_run_id=str(record["run_id"]),
            )
            return self._record_evidence(record, job_dir, summary)
        artifact_name = (
            f"remote-ltspice-{record['preview_id']}-{record['run_id']}"
        )
        output = self._run(
            [
                "api",
                f"repos/{record['repository']}/actions/runs/{record['run_id']}/artifacts",
            ]
        )
        try:
            listing = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub artifact listing returned invalid JSON") from exc
        artifacts_list = listing.get("artifacts") if isinstance(listing, dict) else None
        matches = [
            item
            for item in artifacts_list or []
            if isinstance(item, dict)
            and item.get("name") == artifact_name
            and not item.get("expired")
        ]
        if len(matches) != 1:
            raise RuntimeError("the expected remote evidence artifact is unavailable")
        size = matches[0].get("size_in_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_REMOTE_ARTIFACT_BYTES:
            raise RuntimeError("remote evidence artifact exceeds the download limit")
        temporary = Path(tempfile.mkdtemp(prefix=".evidence-", dir=job_dir))
        try:
            self._run(
                [
                    "run",
                    "download",
                    str(record["run_id"]),
                    "--repo",
                    str(record["repository"]),
                    "--name",
                    artifact_name,
                    "--dir",
                    str(temporary),
                ]
            )
            summary = remote_study.verify_remote_evidence(
                temporary,
                expected_preview_id=str(record["preview_id"]),
                expected_preview_sha256=str(record["preview_sha256"]),
                expected_run_id=str(record["run_id"]),
            )
            os.replace(temporary, final)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return self._record_evidence(record, job_dir, summary)

    def _record_evidence(
        self,
        record: dict[str, object],
        job_dir: Path,
        summary: dict[str, object],
    ) -> dict[str, object]:
        reports: list[dict[str, str]] = []
        experiments = summary.get("experiments", [])
        if isinstance(experiments, list):
            for experiment in experiments:
                if not isinstance(experiment, dict):
                    continue
                report = experiment.get("report_html")
                name = experiment.get("name")
                if isinstance(report, str) and isinstance(name, str):
                    reports.append(
                        {
                            "name": name,
                            "url": (
                                f"/evidence/remote-jobs/{record['remote_job_id']}/"
                                f"evidence/{report}"
                            ),
                        }
                    )
        record.update(
            {
                "state": "evidence_verified",
                "evidence_available": True,
                "reports": reports,
                "updated_at": _utc_now(),
            }
        )
        self._write_record(job_dir, record)
        return record
