[CmdletBinding()]
param(
    [string]$Workspace = $PSScriptRoot,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = $PSScriptRoot
$workspacePath = Resolve-Path -LiteralPath $Workspace -ErrorAction Stop
if (-not (Test-Path -LiteralPath $workspacePath -PathType Container)) {
    throw "System Builder workspace is not a directory: $workspacePath"
}

function Find-CompatiblePython {
    $candidates = @(
        @{ Command = "py"; Arguments = @("-3") },
        @{ Command = "python"; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        $command = [string]$candidate.Command
        $prefixArguments = [string[]]$candidate.Arguments
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            continue
        }
        & $command @prefixArguments -c `
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" `
            2>$null
        if ($LASTEXITCODE -eq 0) {
            return $candidate
        }
    }
    throw @"
Python 3.13 or newer was not found. Install it without administrator access,
then run this launcher again:

    winget install --id Python.Python.3.13
"@
}

function Find-LTspice {
    $configured = $env:LTSPICE_EXECUTABLE
    if ($configured) {
        if (Test-Path -LiteralPath $configured -PathType Leaf) {
            return (Resolve-Path -LiteralPath $configured).Path
        }
        Write-Warning "LTSPICE_EXECUTABLE does not name a file: $configured"
    }

    $candidates = @()
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "ADI\LTspice\LTspice.exe"
        $candidates += Join-Path $env:ProgramFiles "LTC\LTspiceXVII\XVIIx64.exe"
    }
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\ADI\LTspice\LTspice.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "LTC\LTspiceIV\scad3.exe"
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

$venvRoot = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $python = Find-CompatiblePython
    $pythonCommand = [string]$python.Command
    $pythonArguments = [string[]]$python.Arguments
    Write-Host "Creating the local Python environment..."
    & $pythonCommand @pythonArguments -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Python failed to create $venvRoot"
    }
}

& $venvPython -c `
    "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" `
    2>$null
if ($LASTEXITCODE -ne 0) {
    throw @"
The existing .venv uses an unsupported Python version. Remove this directory,
then run the launcher again so it can create a Python 3.13+ environment:

    $venvRoot
"@
}

$requirementFiles = @(
    (Join-Path $projectRoot "requirements-gui.txt"),
    (Join-Path $projectRoot "requirements-mcp.txt")
)
$requirementsFingerprint = (& $venvPython -c `
    "import hashlib,pathlib,sys; print(hashlib.sha256(b''.join(pathlib.Path(p).read_bytes() for p in sys.argv[1:])).hexdigest())" `
    @requirementFiles).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Failed to fingerprint the System Builder requirements"
}
$requirementsMarker = Join-Path $venvRoot ".system-builder-requirements"
$installedFingerprint = if (Test-Path -LiteralPath $requirementsMarker -PathType Leaf) {
    (Get-Content -LiteralPath $requirementsMarker -Raw).Trim()
} else {
    ""
}

$importsAvailable = (& $venvPython -c `
    "import importlib.util; names=('fastapi','httpx','mcp','uvicorn'); print('1' if all(importlib.util.find_spec(name) for name in names) else '0')"
).Trim() -eq "1"
if (-not $importsAvailable -or $installedFingerprint -ne $requirementsFingerprint) {
    Write-Host "Installing System Builder dependencies into .venv..."
    & $venvPython -m pip install --disable-pip-version-check `
        -r (Join-Path $projectRoot "requirements-gui.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "System Builder dependency installation failed"
    }
    Set-Content -LiteralPath $requirementsMarker `
        -Value $requirementsFingerprint -Encoding ASCII -NoNewline
}

$pythonVersion = & $venvPython -c `
    "import platform; print(platform.python_version())"
Write-Host "Python: $pythonVersion"

$ltspice = Find-LTspice
if ($ltspice) {
    $env:LTSPICE_EXECUTABLE = $ltspice
    Write-Host "LTspice: $ltspice"
    Write-Host "First installation only: open LTspice once and answer its usage-data prompt."
} else {
    Remove-Item Env:LTSPICE_EXECUTABLE -ErrorAction SilentlyContinue
    Write-Warning @"
LTspice was not found. Recipe editing and plan preview remain available, but
simulation and schematic capture will fail until LTspice is installed:

    winget install --id AnalogDevices.LTspice

After installation, open LTspice once and answer its usage-data prompt.
"@
}

$arguments = @(
    (Join-Path $projectRoot "system_builder.py"),
    "--workspace",
    $workspacePath.Path
)
if ($NoBrowser) {
    $arguments += "--no-browser"
}

Write-Host "Workspace: $($workspacePath.Path)"
& $venvPython @arguments
exit $LASTEXITCODE
