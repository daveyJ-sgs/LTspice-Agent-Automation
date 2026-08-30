[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Archive
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$testRoot = Join-Path $env:RUNNER_TEMP "system-builder-bundle-smoke"
if (Test-Path -LiteralPath $testRoot) {
    throw "Bundle smoke directory already exists: $testRoot"
}
$extractRoot = Join-Path $testRoot "extracted"
$workspace = Join-Path $testRoot "workspace"
$stdout = Join-Path $testRoot "stdout.log"
$stderr = Join-Path $testRoot "stderr.log"
New-Item -ItemType Directory -Path $extractRoot | Out-Null
New-Item -ItemType Directory -Path $workspace | Out-Null
Expand-Archive -LiteralPath $Archive -DestinationPath $extractRoot

$executable = Get-ChildItem -LiteralPath $extractRoot `
    -Filter "LTspice-System-Builder.exe" -File -Recurse |
    Select-Object -First 1
if (-not $executable) {
    throw "Extracted bundle does not contain LTspice-System-Builder.exe"
}
$pythonExecutables = Get-ChildItem -LiteralPath $extractRoot `
    -Filter "python.exe" -File -Recurse
if ($pythonExecutables) {
    throw "Extracted bundle unexpectedly contains python.exe"
}

$savedPath = $env:PATH
$savedPythonHome = $env:PYTHONHOME
$savedPythonPath = $env:PYTHONPATH
$process = $null
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    $process = Start-Process $executable.FullName -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
        -ArgumentList @("--workspace", "`"$workspace`"", "--no-browser")

    $deadline = (Get-Date).AddMinutes(3)
    $url = $null
    while ((Get-Date) -lt $deadline -and -not $url) {
        Start-Sleep -Milliseconds 250
        $process.Refresh()
        if (Test-Path -LiteralPath $stdout) {
            $match = Select-String -Path $stdout `
                -Pattern "LTspice System Builder: (http://127\.0\.0\.1:\d+/)" |
                Select-Object -Last 1
            if ($match) {
                $url = $match.Matches[0].Groups[1].Value
            }
        }
        if ($process.HasExited -and -not $url) {
            $diagnostic = @()
            if (Test-Path $stdout) { $diagnostic += Get-Content $stdout }
            if (Test-Path $stderr) { $diagnostic += Get-Content $stderr }
            throw "Packaged System Builder exited before startup:`n$($diagnostic -join "`n")"
        }
    }
    if (-not $url) {
        throw "Packaged System Builder did not report a URL before timeout"
    }

    $health = Invoke-RestMethod -Uri ($url + "health") -TimeoutSec 10
    if ($health.status -ne "ok" -or $health.mode -ne "local-only") {
        throw "Unexpected System Builder health response: $($health | ConvertTo-Json -Compress)"
    }
    Write-Host "Extracted Windows bundle smoke passed: $url"
} finally {
    $env:PATH = $savedPath
    if ($null -ne $savedPythonHome) { $env:PYTHONHOME = $savedPythonHome }
    if ($null -ne $savedPythonPath) { $env:PYTHONPATH = $savedPythonPath }
    if ($process -and -not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F | Out-Null
    }
}
