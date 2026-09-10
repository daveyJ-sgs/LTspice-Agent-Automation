#!/usr/bin/env python3
"""Packaged Windows entry point for LTspice System Builder."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

import system_builder

WORKSPACE_NAME = "LTspice System Builder Workspace"


def default_workspace() -> Path:
    """Return the writable workspace used by the packaged application."""
    profile = Path(os.environ.get("USERPROFILE", Path.home()))
    return profile / "Documents" / WORKSPACE_NAME


def packaged_arguments(arguments: Sequence[str]) -> list[str]:
    """Supply and initialize the packaged default workspace when omitted."""
    resolved = list(arguments)
    if any(value == "--workspace" or value.startswith("--workspace=") for value in resolved):
        return resolved
    workspace = default_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    return ["--workspace", str(workspace), *resolved]


def main() -> None:
    sys.argv[1:] = packaged_arguments(sys.argv[1:])
    system_builder.main()


if __name__ == "__main__":
    main()
