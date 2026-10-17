# Codex-Memory Wrapper: compatibility fallback for Codex CLI sessions.
#
# Official hooks are the primary integration. This wrapper remains functional
# for older Codex versions, but its transcript-free exit capture is
# heartbeat-only unless an explicit stable transcript path is supplied.
#
# Install (one-time, in your PowerShell profile):
#   1. Open your profile: `notepad $PROFILE`
#      (if it doesn't exist: `New-Item -Path $PROFILE -ItemType File -Force`)
#   2. Add this line:
#      . "$env:LLM_WIKI_ROOT\scripts\codex-memory-wrapper.ps1"
#   3. Restart your terminal.
#
# After install, every time you run `codex` from any directory, the
# wrapper intercepts the command, runs Codex normally, and AFTER Codex
# exits, automatically captures the session into the LLM-wiki memory
# pipeline. No manual steps required.
#
# Mechanism: this script defines a function `codex` that shadows the
# real `codex.ps1` from npm. The wrapper invokes the real binary by
# its full path, then calls codex_memory.py in a `finally` block so
# the memory capture happens even if Codex crashes or is interrupted.
#
# To disable temporarily: `codex -NoMemory ...` runs without capture.
# To check status: `codex-memory-status` shows recent captures.

# Resolve the real codex binary (npm shim, full path to avoid recursion).
$REAL_CODEX = if ($IsWindows -or $PSVersionTable.Platform -eq $null) {
    # Windows: npm installs codex.cmd in %APPDATA%\npm
    if (Test-Path "$env:APPDATA\npm\codex.cmd") {
        "$env:APPDATA\npm\codex.cmd"
    } elseif (Test-Path "$env:APPDATA\npm\codex.ps1") {
        "$env:APPDATA\npm\codex.ps1"
    } else {
        # Fallback to whatever's on PATH that isn't this wrapper.
        (Get-Command codex.cmd -ErrorAction SilentlyContinue).Source
    }
} else {
    # Unix: codex is typically in /usr/local/bin or ~/.local/bin.
    # Exclude Function/Script results to avoid resolving our own wrapper
    # when the profile is reloaded (recursion guard).
    $app = Get-Command codex -All -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandType -eq 'Application' } |
        Select-Object -First 1
    if ($app) { $app.Source }
}

if (-not $REAL_CODEX -or -not (Test-Path $REAL_CODEX)) {
    Write-Warning "codex-memory-wrapper: could not locate real codex binary; wrapper disabled."
    return
}

<#
.SYNOPSIS
  Wraps the `codex` command with automatic LLM-wiki memory capture.
.DESCRIPTION
  Runs the real Codex CLI, then on exit invokes codex_memory.py to
  flush the session into the memory pipeline. Honors -NoMemory to skip.
.EXAMPLE
  codex "refactor the auth module"
  # Codex runs normally; memory capture happens automatically after exit.
.EXAMPLE
  codex -NoMemory "quick one-off question"
  # Codex runs; memory capture SKIPPED for this session.
#>
# Context generation happens before the real process; failures remain visible.
function Initialize-CodexMemoryContext {
    param([string]$Directory)
    try {
        $contextFile = Join-Path $env:LLM_WIKI_STATE_ROOT "cache\session-context.md"
        & uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python `
            "$env:LLM_WIKI_ROOT\scripts\session_start_context.py" --output-file $contextFile | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "session context generation failed ($LASTEXITCODE)" }
        # codex_memory.py project-state recovers journals before context injection.
        & uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python `
            "$env:LLM_WIKI_ROOT\scripts\codex_memory.py" project-state --cwd $Directory |
            Add-Content -LiteralPath $contextFile -Encoding utf8
        if ($LASTEXITCODE -ne 0) { throw "project context generation failed ($LASTEXITCODE)" }
    } catch { Write-Warning "[codex-memory] context unavailable: $_" }
}

function Invoke-CodexMemoryMaintenance {
    & uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python scripts/memory_queue.py work 2>&1 |
        ForEach-Object { Write-Host "[codex-memory] $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) { Write-Warning "[codex-memory] queue work failed ($LASTEXITCODE)" }
    & uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python scripts/maybe_compile.py 2>&1 |
        ForEach-Object { Write-Host "[codex-memory] $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) { Write-Warning "[codex-memory] compile trigger failed ($LASTEXITCODE)" }
}

function Save-CodexMemorySession {
    param([string]$Directory, [string]$Reason)
    Push-Location $env:LLM_WIKI_ROOT
    try {
        & uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python scripts/codex_memory.py daily-log `
            --cwd $Directory --reason $Reason --json | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "session capture failed ($LASTEXITCODE)" }
        Invoke-CodexMemoryMaintenance
    } finally { Pop-Location }
}

function Complete-CodexMemorySession {
    param([string]$Directory, [string[]]$Arguments, [switch]$NoMemory)
    if ($NoMemory) { return }
    $reason = if ($Arguments -contains 'exec') { 'codex-exec' } else { 'codex-session-end' }
    try { Save-CodexMemorySession -Directory $Directory -Reason $reason }
    catch { Write-Warning "[codex-memory] session capture incomplete: $_" }
}

function codex {
    [CmdletBinding()]
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments,
        [switch]$NoMemory
    )
    $fwdArgs = $Arguments | Where-Object { $_ -ne '-NoMemory' -and $_ -ne '--NoMemory' }
    $cwdBefore = (Get-Location).Path
    $exitCode = 1
    try {
        Initialize-CodexMemoryContext -Directory $cwdBefore
        & $REAL_CODEX @fwdArgs
        $exitCode = $LASTEXITCODE
    } catch { Write-Error "codex failed: $_" }
    finally {
        Complete-CodexMemorySession -Directory $cwdBefore -Arguments $fwdArgs -NoMemory:$NoMemory
        $global:LASTEXITCODE = $exitCode
    }
}

function Get-CodexMemoryState {
    $statePath = Join-Path $env:LLM_WIKI_STATE_ROOT "run\state.json"
    if (-not (Test-Path -LiteralPath $statePath)) {
        Write-Host "  (state.json not found - no captures yet)"
        return $null
    }
    try { return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json }
    catch {
        Write-Host "  (state.json corrupt or unreadable: $($_.Exception.Message))" -ForegroundColor Yellow
        return $null
    }
}

function Write-CodexMemoryHeartbeats {
    param($State)
    Write-Host "=== Codex heartbeats (recent activity) ===" -ForegroundColor Cyan
    if (-not $State.codex_heartbeats) { Write-Host "  (no heartbeats yet)"; return }
    $State.codex_heartbeats.PSObject.Properties | ForEach-Object {
        $h = $_.Value
        Write-Host "  $($_.Name): $($h.reason) at $($h.at)"
    }
}

function Write-CodexMemoryCompileStatus {
    param($State)
    Write-Host "`n=== Tier distribution ===" -ForegroundColor Cyan
    if ($State.flush_tier_counts) {
        $State.flush_tier_counts.PSObject.Properties | ForEach-Object {
            Write-Host "  $($_.Name): $($_.Value)"
        }
    }
    Write-Host "`n=== Last compile ===" -ForegroundColor Cyan
    Write-Host "  at: $($State.last_compile_at)"
    Write-Host "  status: $($State.last_compile_status)"
    if ($State.last_compile_audit) {
        Write-Host "  verified citations: $($State.last_compile_audit.verified)"
    }
}

function Write-CodexMemoryQueueStatus {
    Write-Host "`n=== Memory queue (deferred tasks) ===" -ForegroundColor Cyan
    $queueStatus = & uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python scripts/memory_queue.py status | Out-String
    if ($LASTEXITCODE -ne 0) { Write-Warning "[codex-memory] queue status unavailable ($LASTEXITCODE)"; return }
    Write-Host $queueStatus
}

function codex-memory-status {
    Push-Location $env:LLM_WIKI_ROOT
    try {
        $state = Get-CodexMemoryState
        if ($null -eq $state) { return }
        Write-CodexMemoryHeartbeats -State $state
        Write-CodexMemoryCompileStatus -State $state
        Write-Host "`n=== Daily log count ===" -ForegroundColor Cyan
        $dailies = Get-ChildItem -LiteralPath (Join-Path $env:LLM_WIKI_ROOT 'knowledge\daily') -Filter *.md -ErrorAction SilentlyContinue
        Write-Host "  total daily logs: $($dailies.Count)"
        Write-CodexMemoryQueueStatus
    } finally { Pop-Location }
}

<#
.SYNOPSIS
  Run compile_memory.py to convert pending daily logs into knowledge pages.
.DESCRIPTION
  Convenience wrapper. Runs the LLM compile pipeline, then shows the
  audit summary. Equivalent to:
    cd $env:LLM_WIKI_ROOT; uv run python scripts/compile_memory.py
#>
function codex-memory-compile {
    [CmdletBinding()]
    param(
        [switch]$All
    )
    Push-Location $env:LLM_WIKI_ROOT
    try {
        $pyArgs = @(
            "run", "--locked", "--no-sync", "--directory", $env:LLM_WIKI_ROOT,
            "python", "scripts/compile_memory.py"
        )
        if ($All) { $pyArgs += "--all" }
        & uv @pyArgs
    }
    finally {
        Pop-Location
    }
}

Write-Host "[codex-memory-wrapper] Loaded. 'codex' is now wrapped with auto-capture." -ForegroundColor DarkGray
Write-Host "[codex-memory-wrapper] Commands: codex (auto), codex-memory-status, codex-memory-compile" -ForegroundColor DarkGray
