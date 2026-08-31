#!/bin/bash
# Double-click launcher for LTspice System Builder on macOS.
#
# Mirrors Start-SystemBuilder.ps1's contract: no administrator access, no
# machine-wide changes. It creates a private .venv inside the repository if
# needed, installs the declared GUI dependencies, diagnoses LTspice
# discovery, and opens System Builder in the default browser on a random
# loopback-only port.
set -eo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

WORKSPACE="$PROJECT_ROOT"
NO_BROWSER_FLAG=""
for arg in "$@"; do
    case "$arg" in
        --workspace=*)
            WORKSPACE="${arg#*=}"
            ;;
        --no-browser)
            NO_BROWSER_FLAG="--no-browser"
            ;;
    esac
done

is_python_313_or_newer() {
    "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" \
        >/dev/null 2>&1
}

find_compatible_python() {
    for candidate in python3.14 python3.13 python3; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && is_python_313_or_newer "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

VENV_ROOT="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_ROOT/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    if ! PYTHON_BIN="$(find_compatible_python)"; then
        cat <<'EOF' >&2
Python 3.13 or newer was not found. Install it without administrator access,
then run this launcher again:

    brew install python@3.13
EOF
        exit 1
    fi
    echo "Creating the local Python environment..."
    "$PYTHON_BIN" -m venv "$VENV_ROOT"
fi

if ! is_python_313_or_newer "$VENV_PYTHON"; then
    cat <<EOF >&2
The existing .venv uses an unsupported Python version. Remove this directory,
then run the launcher again so it can create a Python 3.13+ environment:

    $VENV_ROOT
EOF
    exit 1
fi

REQUIREMENTS_MARKER="$VENV_ROOT/.system-builder-requirements"
REQUIREMENTS_FINGERPRINT="$("$VENV_PYTHON" -c "
import hashlib, pathlib, sys
print(hashlib.sha256(b''.join(pathlib.Path(p).read_bytes() for p in sys.argv[1:])).hexdigest())
" "$PROJECT_ROOT/requirements-gui.txt" "$PROJECT_ROOT/requirements-mcp.txt")"
INSTALLED_FINGERPRINT=""
if [ -f "$REQUIREMENTS_MARKER" ]; then
    INSTALLED_FINGERPRINT="$(cat "$REQUIREMENTS_MARKER")"
fi

IMPORTS_AVAILABLE="$("$VENV_PYTHON" -c "
import importlib.util
names = ('fastapi', 'httpx', 'mcp', 'uvicorn')
print('1' if all(importlib.util.find_spec(name) for name in names) else '0')
")"

if [ "$IMPORTS_AVAILABLE" != "1" ] || [ "$INSTALLED_FINGERPRINT" != "$REQUIREMENTS_FINGERPRINT" ]; then
    echo "Installing System Builder dependencies into .venv..."
    "$VENV_PYTHON" -m pip install --disable-pip-version-check \
        -r "$PROJECT_ROOT/requirements-gui.txt"
    printf '%s' "$REQUIREMENTS_FINGERPRINT" >"$REQUIREMENTS_MARKER"
fi

echo "Python: $("$VENV_PYTHON" -c 'import platform; print(platform.python_version())')"

LTSPICE_PATH="${LTSPICE_EXECUTABLE:-}"
if [ -n "$LTSPICE_PATH" ] && [ ! -f "$LTSPICE_PATH" ]; then
    echo "Warning: LTSPICE_EXECUTABLE does not name a file: $LTSPICE_PATH" >&2
    LTSPICE_PATH=""
fi
if [ -z "$LTSPICE_PATH" ] && [ -f "/Applications/LTspice.app/Contents/MacOS/LTspice" ]; then
    LTSPICE_PATH="/Applications/LTspice.app/Contents/MacOS/LTspice"
fi

if [ -n "$LTSPICE_PATH" ]; then
    export LTSPICE_EXECUTABLE="$LTSPICE_PATH"
    echo "LTspice: $LTSPICE_PATH"
    echo "First installation only: open LTspice once and answer its usage-data prompt."
else
    unset LTSPICE_EXECUTABLE
    cat <<'EOF' >&2
Warning: LTspice was not found. Recipe editing and plan preview remain
available, but simulation and schematic capture will fail until LTspice is
installed:

    brew install --cask ltspice

After installation, open LTspice once and answer its usage-data prompt.
EOF
fi

echo "Workspace: $WORKSPACE"
"$VENV_PYTHON" "$PROJECT_ROOT/system_builder.py" --workspace "$WORKSPACE" $NO_BROWSER_FLAG
STATUS=$?
echo ""
echo "System Builder stopped. Press Return to close this window."
read -r _
exit $STATUS
