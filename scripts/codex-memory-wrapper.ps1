# Codex-Memory Wrapper: automatic memory capture for Codex CLI sessions.
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
    # Unix: codex is typically in /usr/local/bin or ~/.local/bin
    (Get-Command codex -ErrorAction SilentlyContinue).Source
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
function codex {
    [CmdletBinding()]
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments,
        [switch]$NoMemory
    )

    # Strip -NoMemory from the args we forward to real codex.
    $fwdArgs = $Arguments | Where-Object { $_ -ne '-NoMemory' -and $_ -ne '--NoMemory' }

    # Remember the cwd BEFORE codex runs (codex may cd internally).
    $cwdBefore = (Get-Location).Path

    # Set CODEX_MEMORY_INVOKED so the wrapper can detect nesting.
    $env:CODEX_MEMORY_INVOKED = "1"

    try {
        # Invoke the real codex binary with all forwarded args.
        & $REAL_CODEX @fwdArgs
        $exitCode = $LASTEXITCODE
    }
    catch {
        Write-Error "codex failed: $_"
        $exitCode = 1
    }
    finally {
        # Always run memory capture, even if codex crashed or was Ctrl-C'd.
        if (-not $NoMemory) {
            try {
                $reason = if ($fwdArgs -contains 'exec') { 'codex-exec' } else { 'codex-session-end' }
                Push-Location $env:LLM_WIKI_ROOT
                & uv run python scripts/codex_memory.py daily-log `
                    --cwd $cwdBefore `
                    --reason $reason `
                    --json 2>$null | Out-Null

                # Drain pending memory-pipeline queue: any tasks that were
                # enqueued while no backend was available get serviced now
                # by Codex LLM (via llm_client.py auto-detection).
                $drainResult = & uv run python scripts/memory_queue.py drain 2>&1
                if ($drainResult -match "drain complete") {
                    Write-Host "[codex-memory] $drainResult" -ForegroundColor DarkGray
                }

                # Fire-and-forget compile trigger. Checks concurrency lock
                # + pending work; spawns detached compile_memory.py if both
                # pass. Returns immediately — compile runs in background
                # and won't block the next codex invocation.
                & uv run python scripts/maybe_compile.py 2>&1 | ForEach-Object {
                    Write-Host "[codex-memory] $_" -ForegroundColor DarkGray
                }

                Pop-Location
            }
            catch {
                # Never let memory capture failure block the user.
            }
        }
        Remove-Item Env:\CODEX_MEMORY_INVOKED -ErrorAction SilentlyContinue
    }

    # Propagate codex's exit code.
    exit $exitCode
}

<#
.SYNOPSIS
  Show recent Codex memory captures and current backlog.
#>
function codex-memory-status {
    Push-Location $env:LLM_WIKI_ROOT
    try {
        Write-Host "=== Codex heartbeats (recent activity) ===" -ForegroundColor Cyan
        $state = Get-Content "$env:LLM_WIKI_ROOT-state\memory-state\state.json" -Raw | ConvertFrom-Json
        if ($state.codex_heartbeats) {
            $state.codex_heartbeats.PSObject.Properties | ForEach-Object {
                $h = $_.Value
                Write-Host "  $($_.Name): $($h.reason) at $($h.at)"
            }
        } else {
            Write-Host "  (no heartbeats yet)"
        }

        Write-Host ""
        Write-Host "=== Tier distribution ===" -ForegroundColor Cyan
        if ($state.flush_tier_counts) {
            $state.flush_tier_counts.PSObject.Properties | ForEach-Object {
                Write-Host "  $($_.Name): $($_.Value)"
            }
        }

        Write-Host ""
        Write-Host "=== Last compile ===" -ForegroundColor Cyan
        Write-Host "  at: $($state.last_compile_at)"
        Write-Host "  status: $($state.last_compile_status)"
        if ($state.last_compile_audit) {
            Write-Host "  verified citations: $($state.last_compile_audit.verified)"
        }

        Write-Host ""
        Write-Host "=== Daily log count ===" -ForegroundColor Cyan
        $dailies = Get-ChildItem $env:LLM_WIKI_ROOT\memory\daily\*.md -ErrorAction SilentlyContinue
        Write-Host "  total daily logs: $($dailies.Count)"

        Write-Host ""
        Write-Host "=== Memory queue (deferred tasks) ===" -ForegroundColor Cyan
        Push-Location $env:LLM_WIKI_ROOT
        try {
            $queueStatus = & uv run python scripts/memory_queue.py status 2>$null | Out-String
            if ($queueStatus) { Write-Host $queueStatus }
            else { Write-Host "  (queue empty or memory_queue unavailable)" }
        } finally { Pop-Location }
    }
    finally {
        Pop-Location
    }
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
        $args = @("run", "python", "scripts/compile_memory.py")
        if ($All) { $args += "--all" }
        & uv @args
    }
    finally {
        Pop-Location
    }
}

Write-Host "[codex-memory-wrapper] Loaded. 'codex' is now wrapped with auto-capture." -ForegroundColor DarkGray
Write-Host "[codex-memory-wrapper] Commands: codex (auto), codex-memory-status, codex-memory-compile" -ForegroundColor DarkGray
