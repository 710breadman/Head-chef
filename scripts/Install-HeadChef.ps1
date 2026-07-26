[CmdletBinding()]
param(
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "Head Chef installer"
Write-Host "Project: $Root"

try {
    & $Python -3.11 --version | Out-Host
    $PythonArgs = @("-3.11")
}
catch {
    $FallbackVersion = (& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
    if ([version]$FallbackVersion -lt [version]"3.11") {
        throw "Python 3.11 or newer required; found $FallbackVersion"
    }
    & python --version | Out-Host
    $Python = "python"
    $PythonArgs = @()
}

if (-not (Test-Path ".venv")) {
    & $Python @PythonArgs -m venv .venv
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$SitePackages = (& $VenvPython -c "import site; print(site.getsitepackages()[0])").Trim()
$PathFile = Join-Path $SitePackages "head_chef_repo.pth"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($PathFile, ((Join-Path $Root "src") + [Environment]::NewLine), $Utf8NoBom)

$CommandFile = Join-Path $Root ".venv\Scripts\head-chef.cmd"
$CommandBody = @'
@echo off
"%~dp0python.exe" -m head_chef %*
'@
Set-Content -LiteralPath $CommandFile -Value $CommandBody -Encoding ASCII

& $VenvPython -m head_chef init $Root

Write-Host ""
Write-Host "Installed without downloading Python packages. Next command:"
Write-Host ".\.venv\Scripts\head-chef.cmd doctor"
