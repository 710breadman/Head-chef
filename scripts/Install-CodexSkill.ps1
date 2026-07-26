[CmdletBinding()]
param(
    [string]$CodexHome = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $Root "skills\head-chef-local-router"
if (-not $CodexHome) {
    $CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
}
$SkillsRoot = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) "skills"
$Destination = Join-Path $SkillsRoot "head-chef-local-router"

if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "Skill source not found: $Source"
}
New-Item -ItemType Directory -Force -Path $SkillsRoot | Out-Null
if (Test-Path -LiteralPath $Destination) {
    throw "Skill already exists: $Destination. Remove or update it intentionally."
}
Copy-Item -LiteralPath $Source -Destination $Destination -Recurse
Write-Host "Installed Codex skill: $Destination"
Write-Host "Restart Codex, then invoke `$head-chef-local-router."
