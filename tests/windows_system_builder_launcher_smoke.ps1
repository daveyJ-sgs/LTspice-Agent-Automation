$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot
$stdout = Join-Path $env:RUNNER_TEMP "system-builder-launcher.stdout.log"
$stderr = Join-Path $env:RUNNER_TEMP "system-builder-launcher.stderr.log"
$launcher = Join-Path $root "Start-SystemBuilder.ps1"
$process = $null

try {
    $process = Start-Process powershell.exe -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
        -ArgumentList @(
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$launcher`"",
            "-Workspace", "`"$root`"",
            "-NoBrowser"
        )

    $deadline = (Get-Date).AddMinutes(3)
    $url = $null
    while ((Get-Date) -lt $deadline -and -not $url) {
        Start-Sleep -Milliseconds 250
        $process.Refresh()
        if (Test-Path $stdout) {
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
            throw "System Builder launcher exited before startup:`n$($diagnostic -join "`n")"
        }
    }
    if (-not $url) {
        throw "System Builder launcher did not report a URL before timeout"
    }

    $health = Invoke-RestMethod -Uri ($url + "health") -TimeoutSec 10
    if ($health.status -ne "ok") {
        throw "Unexpected System Builder health response: $($health | ConvertTo-Json -Compress)"
    }
    Write-Host "Windows launcher smoke passed: $url"
} finally {
    if ($process -and -not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F | Out-Null
    }
}
