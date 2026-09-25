#!/usr/bin/env python3
"""Packaged Windows entry point for LTspice System Builder."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Sequence

import system_builder

WORKSPACE_NAME = "LTspice System Builder Workspace"


# FOLDERID_Documents, the shell's "Documents" known folder.
_FOLDERID_DOCUMENTS = uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")


def _known_documents_folder() -> Path | None:
    """Ask the Windows shell where Documents really is.

    OneDrive folder backup and Group Policy redirect Documents away from
    %USERPROFILE%\\Documents, so the profile-relative guess can name a folder
    the user never sees. Returns None off Windows or on any failure.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        folder_id = _GUID.from_buffer_copy(_FOLDERID_DOCUMENTS.bytes_le)
        path = ctypes.c_wchar_p()
        windll = getattr(ctypes, "windll")
        result = windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(path)
        )
        try:
            if result != 0 or not path.value:
                return None
            return Path(path.value)
        finally:
            # The shell allocates the string even on some failures.
            windll.ole32.CoTaskMemFree(path)
    except (AttributeError, OSError, ValueError):
        return None


def default_workspace() -> Path:
    """Return the writable workspace used by the packaged application."""
    documents = _known_documents_folder()
    if documents is None:
        profile = Path(os.environ.get("USERPROFILE", Path.home()))
        documents = profile / "Documents"
    return documents / WORKSPACE_NAME


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
