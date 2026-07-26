[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$Command = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) "bin\head-chef.cmd"
if (-not (Test-Path -LiteralPath $Command -PathType Leaf)) {
    throw "Head Chef launcher missing. Run the repository's scripts\Install-CodexSkill.ps1 first."
}
& $Command @Arguments
exit $LASTEXITCODE
