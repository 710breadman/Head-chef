[CmdletBinding()]
param(
    [string]$CodexHome = "",
    [string]$Python = "py",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $Root "skills\head-chef-local-router"
if (-not $CodexHome) {
    $CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
}
$ResolvedCodexHome = [System.IO.Path]::GetFullPath($CodexHome)
$SkillsRoot = Join-Path $ResolvedCodexHome "skills"
$BinRoot = Join-Path $ResolvedCodexHome "bin"
$ToolsRoot = Join-Path $ResolvedCodexHome "tools"
$Destination = Join-Path $SkillsRoot "head-chef-local-router"
$Staging = Join-Path $SkillsRoot (".head-chef-local-router.install-" + $PID)
$RuntimeRoot = Join-Path $ToolsRoot "head-chef"
$RuntimeStaging = Join-Path $ToolsRoot (".head-chef.install-" + $PID)

if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "Skill source not found: $Source"
}

if ((Test-Path -LiteralPath $Destination) -or (Test-Path -LiteralPath $RuntimeRoot)) {
    if (-not $Force) {
        throw "Head Chef is already installed under $ResolvedCodexHome. Re-run with -Force to upgrade it."
    }
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmssfff"
New-Item -ItemType Directory -Force -Path $SkillsRoot, $BinRoot, $ToolsRoot | Out-Null
if (Test-Path -LiteralPath $Destination) {
    $SkillBackup = "$Destination.backup-$Timestamp"
    Move-Item -LiteralPath $Destination -Destination $SkillBackup
    Write-Host "Preserved previous skill: $SkillBackup"
}
if (Test-Path -LiteralPath $RuntimeRoot) {
    $RuntimeBackup = "$RuntimeRoot.backup-$Timestamp"
    Move-Item -LiteralPath $RuntimeRoot -Destination $RuntimeBackup
    Write-Host "Preserved previous runtime: $RuntimeBackup"
}

try {
    Copy-Item -LiteralPath $Source -Destination $Staging -Recurse
    Move-Item -LiteralPath $Staging -Destination $Destination

    New-Item -ItemType Directory -Path (Join-Path $RuntimeStaging "src") | Out-Null
    Copy-Item -LiteralPath (Join-Path $Root "src\head_chef") -Destination (Join-Path $RuntimeStaging "src\head_chef") -Recurse
    $RuntimeState = Join-Path $RuntimeStaging "state"
    New-Item -ItemType Directory -Path (Join-Path $RuntimeState "benchmarks") | Out-Null
    $EvidenceFiles = @(
        @("benchmarks\latest.json", "benchmarks\latest.json"),
        @("model-overrides.json", "model-overrides.json"),
        @("outcomes.jsonl", "outcomes.jsonl")
    )
    foreach ($Evidence in $EvidenceFiles) {
        $EvidenceSource = Join-Path (Join-Path $Root ".head-chef") $Evidence[0]
        if (Test-Path -LiteralPath $EvidenceSource -PathType Leaf) {
            $EvidenceDestination = Join-Path $RuntimeState $Evidence[1]
            Copy-Item -LiteralPath $EvidenceSource -Destination $EvidenceDestination
        }
    }
    try {
        & $Python -3.11 --version | Out-Host
        & $Python -3.11 -m venv (Join-Path $RuntimeStaging ".venv")
    }
    catch {
        $FallbackVersion = (& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
        if ([version]$FallbackVersion -lt [version]"3.11") {
            throw "Python 3.11 or newer required; found $FallbackVersion"
        }
        & python --version | Out-Host
        & python -m venv (Join-Path $RuntimeStaging ".venv")
    }
    Move-Item -LiteralPath $RuntimeStaging -Destination $RuntimeRoot

    $VenvPython = Join-Path $RuntimeRoot ".venv\Scripts\python.exe"
    $SitePackages = (& $VenvPython -c "import site; print(site.getsitepackages()[-1])").Trim()
    $PathFile = Join-Path $SitePackages "head_chef_runtime.pth"
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $PathFile,
        ((Join-Path $RuntimeRoot "src") + [Environment]::NewLine),
        $Utf8NoBom
    )
    $RuntimeCommand = Join-Path $RuntimeRoot ".venv\Scripts\head-chef.cmd"
    $RuntimeCommandBody = "@echo off`r`n`"%~dp0python.exe`" -m head_chef %*`r`n"
    Set-Content -LiteralPath $RuntimeCommand -Value $RuntimeCommandBody -Encoding ASCII -NoNewline
}
finally {
    if (Test-Path -LiteralPath $Staging) {
        Remove-Item -LiteralPath $Staging -Recurse -Force
    }
    if (Test-Path -LiteralPath $RuntimeStaging) {
        Remove-Item -LiteralPath $RuntimeStaging -Recurse -Force
    }
}

$Shim = Join-Path $BinRoot "head-chef.cmd"
$EscapedRuntime = $RuntimeCommand.Replace("%", "%%")
$EscapedGlobalState = (Join-Path $RuntimeRoot "state").Replace("%", "%%")
$ShimBody = "@echo off`r`nset `"HEAD_CHEF_GLOBAL_STATE=$EscapedGlobalState`"`r`n`"$EscapedRuntime`" %*`r`n"
Set-Content -LiteralPath $Shim -Value $ShimBody -Encoding ASCII -NoNewline

Write-Host "Installed Codex skill: $Destination"
Write-Host "Installed private runtime: $RuntimeRoot"
Write-Host "Installed launcher: $Shim"
Write-Host "Restart Codex, then invoke `$head-chef-local-router."
