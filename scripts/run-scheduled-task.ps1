[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("nightly", "weekly")]
    [string]$Kind,
    [Parameter(Mandatory = $true)][string]$VaultRoot,
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [Parameter(Mandatory = $true)][string]$UvPath
)

$ErrorActionPreference = "Stop"
$env:LLM_WIKI_ROOT = [System.IO.Path]::GetFullPath($VaultRoot)
$env:LLM_WIKI_STATE_ROOT = [System.IO.Path]::GetFullPath($StateRoot)
$scriptName = if ($Kind -eq "nightly") { "scheduled_nightly.py" } else { "scheduled_weekly.py" }
& $UvPath run --locked --no-sync --directory $env:LLM_WIKI_ROOT python `
    (Join-Path $env:LLM_WIKI_ROOT "scripts\$scriptName")
$nativeExit = $LASTEXITCODE
if ($nativeExit -ne 0) { exit $nativeExit }
exit 0
