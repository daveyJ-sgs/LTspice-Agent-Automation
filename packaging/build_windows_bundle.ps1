[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [Parameter(Mandatory = $true)]
    [string]$SourceRevision
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$version = (Get-Content -LiteralPath `
    (Join-Path $PSScriptRoot "system_builder_version.txt") -Raw).Trim()
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
$distRoot = Join-Path $output "dist"
$workRoot = Join-Path $output "build"
$bundleName = "LTspice-System-Builder-$version-windows-x64"
$archive = Join-Path $output "$bundleName.zip"

if (Test-Path -LiteralPath $output) {
    throw "Bundle output directory already exists: $output"
}
New-Item -ItemType Directory -Path $output | Out-Null

python -m PyInstaller --noconfirm --clean `
    --distpath $distRoot --workpath $workRoot `
    (Join-Path $PSScriptRoot "system_builder_windows.spec")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to build System Builder"
}

$bundle = Join-Path $distRoot "LTspice-System-Builder"
$executable = Join-Path $bundle "LTspice-System-Builder.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Packaged executable was not created: $executable"
}

$buildInfo = [ordered]@{
    product = "LTspice System Builder"
    version = $version
    source_revision = $SourceRevision
    target = "windows-x64"
    python = (python -c "import platform; print(platform.python_version())").Trim()
    pyinstaller = (python -m PyInstaller --version).Trim()
}
$buildInfo | ConvertTo-Json | Set-Content -LiteralPath `
    (Join-Path $bundle "BUILD_INFO.json") -Encoding UTF8

Compress-Archive -LiteralPath $bundle -DestinationPath $archive `
    -CompressionLevel Optimal
$archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
$checksum = Join-Path $output "$bundleName.zip.sha256"
Set-Content -LiteralPath $checksum -Encoding ASCII -NoNewline `
    -Value "$archiveHash  $bundleName.zip"

Write-Host "Bundle: $archive"
Write-Host "SHA-256: $archiveHash"
if ($env:GITHUB_OUTPUT) {
    Add-Content -LiteralPath $env:GITHUB_OUTPUT -Encoding UTF8 -Value "archive=$archive"
    Add-Content -LiteralPath $env:GITHUB_OUTPUT -Encoding UTF8 -Value "checksum=$checksum"
    Add-Content -LiteralPath $env:GITHUB_OUTPUT -Encoding UTF8 -Value "bundle_name=$bundleName"
}
