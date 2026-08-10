# install.ps1 - One-command installer for Windows.
#
# Usage from an inspected checkout:
#   git clone https://github.com/Ekgardt/llm-wiki.git
#   cd llm-wiki
#   $env:LLM_WIKI_ROOT = (Get-Location).Path
#   .\install.ps1
#
# Remote bootstrap is not published. The approved replacement will require a
# full commit OID before fetching or executing installer code.
#
# What this does:
#   1. Checks prerequisites (Python 3.10+, uv, git)
#   2. Installs locked Python deps with the MCP server baseline
#   3. Runs tests
#   4. Sets LLM_WIKI_ROOT environment variable (user-level)
#   5. Registers Windows Task Scheduler (nightly + weekly)
#   6. Detects agents (OpenCode, Codex, Claude Code, Cursor)
#   7. Wires up Codex wrapper to PowerShell profile
#   8. Copies OpenCode plugin if OpenCode is installed
#   9. Builds search index
#
# Safe to re-run. Idempotent.

$ErrorActionPreference = "Stop"

function Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Blue }
function Ok($msg)   { Write-Host "[OK] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "[WARN] $msg"  -ForegroundColor Yellow }
function Fail($msg) { Write-Host "[FAIL] $msg"  -ForegroundColor Red; exit 1 }
function Write-Utf8NoBom([string]$path, [string]$content) {
    [System.IO.File]::WriteAllText(
        $path,
        $content,
        [System.Text.UTF8Encoding]::new($false)
    )
}
function Resolve-StateRoot(
    [string]$ProcessState,
    [string]$UserState,
    [string]$VaultRoot
) {
    if (-not [string]::IsNullOrWhiteSpace($ProcessState)) { return $ProcessState }
    if (-not [string]::IsNullOrWhiteSpace($UserState)) { return $UserState }
    return $VaultRoot
}
function Install-CodexHooks(
    [string]$VaultRoot,
    [string]$CodexDir
) {
    & uv run --directory $VaultRoot python (Join-Path $VaultRoot "scripts\codex_memory.py") `
        merge-hooks `
        --source (Join-Path $VaultRoot "integrations\codex\hooks.json") `
        --destination (Join-Path $CodexDir "hooks.json") `
        --config (Join-Path $CodexDir "config.toml") | Out-Null
    $hookExit = $LASTEXITCODE
    if ($hookExit -eq 4) {
        [Console]::Error.WriteLine(
            "Codex lifecycle hooks are disabled. Set [features] hooks = true in config.toml " +
            "and rerun the installer; hooks.json was not changed."
        )
    }
    return $hookExit
}
function Install-CodexMcp(
    [string]$VaultRoot,
    [string]$Config
) {
    $state = (& uv run --directory $VaultRoot python (Join-Path $VaultRoot "scripts\codex_memory.py") `
        config-state --config $Config --vault-root $VaultRoot | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { return 1 }
    if ($state -eq "equivalent") { return 0 }
    if ($state -in @("conflict", "invalid")) { return 2 }
    if ($state -ne "absent") { return 1 }

    $directory = Split-Path $Config -Parent
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $tomlVault = $VaultRoot.Replace("\", "\\").Replace('"', '\"')
    $block = @"
[mcp_servers.llm-wiki]
command = "uv"
args = ["run", "--directory", "$tomlVault", "python", "scripts/mcp_server.py"]
"@
    $encoding = [System.Text.UTF8Encoding]::new($false)
    if (Test-Path $Config) {
        Copy-Item -LiteralPath $Config -Destination "$Config.bak" -Force
        $existing = [System.IO.File]::ReadAllText($Config)
        $separator = if ([string]::IsNullOrEmpty($existing)) {
            ""
        } elseif ($existing.EndsWith("`n")) {
            "`n"
        } else {
            "`n`n"
        }
        [System.IO.File]::AppendAllText($Config, $separator + $block + "`n", $encoding)
    } else {
        [System.IO.File]::WriteAllText($Config, $block + "`n", $encoding)
    }
    return 0
}

# --- 1. Resolve vault root -----------------------------------------

$VAULT_ROOT = if ($env:LLM_WIKI_ROOT) { $env:LLM_WIKI_ROOT } else { $PSScriptRoot }
if (-not (Test-Path "$VAULT_ROOT\pyproject.toml")) {
    Fail "Remote bootstrap is not published. Clone the repository, inspect it, and run this installer from that checkout."
}

Set-Location $VAULT_ROOT
Info "Vault root: $VAULT_ROOT"

# Prevent accidental pushes from the installed vault
git -C $VAULT_ROOT remote set-url --push origin no-push
Ok "Push disabled (no-push) - installed vault cannot push to public remote"

# --- 2. Check prerequisites ---------------------------------------

Info "Checking prerequisites..."

# Python
$pyVersion = (python --version 2>&1) -replace "Python ", ""
$pyParts = $pyVersion.Split(".")
if ([int]$pyParts[0] -lt 3 -or ([int]$pyParts[0] -eq 3 -and [int]$pyParts[1] -lt 10)) {
    Fail "Python 3.10+ required, found $pyVersion"
}
Ok "Python $pyVersion"

# git
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git is required" }
Ok "git installed"

# uv (install if missing)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Info "Installing uv..."
    irm https://astral.sh/uv/install.ps1 | iex
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail "uv installation failed. Install manually: https://docs.astral.sh/uv/"
    }
}
Ok "uv installed"

# --- 3. Install dependencies --------------------------------------

Info "Installing locked Python dependencies with MCP support..."
uv sync --locked --extra mcp-server --quiet
Ok "Dependencies installed (MCP server baseline included)"

# --- 4. Run tests -------------------------------------------------

Info "Running test suite..."
$testProcess = $null
try {
    $testProcess = Start-Process `
        -FilePath "uv" `
        -ArgumentList "run pytest -q" `
        -NoNewWindow `
        -PassThru
    # Windows PowerShell 5.1 needs an open handle to retain a fast process's exit code.
    $null = $testProcess.Handle
    $testProcess.WaitForExit()
    $testExit = $testProcess.ExitCode
    if ($testExit -ne 0) {
        Warn "Some tests failed - core features will still work, but please report issues"
    } else {
        Ok "Test suite passed"
    }
} finally {
    if ($null -ne $testProcess) {
        if (-not $testProcess.HasExited) {
            $testPid = [int]$testProcess.Id
            $taskkillProcess = $null
            try {
                $taskkillPath = Join-Path $env:SystemRoot "System32\taskkill.exe"
                $taskkillProcess = Start-Process `
                    -FilePath $taskkillPath `
                    -ArgumentList @("/PID", [string]$testPid, "/T", "/F") `
                    -NoNewWindow `
                    -PassThru
                $null = $taskkillProcess.Handle
                if (-not $taskkillProcess.WaitForExit(10000)) {
                    $taskkillProcess.Kill()
                    $null = $taskkillProcess.WaitForExit(5000)
                }
            } catch {
                # The process may have exited between HasExited and taskkill.
            } finally {
                if ($null -ne $taskkillProcess) {
                    $taskkillProcess.Close()
                }
            }
            if (-not $testProcess.HasExited) {
                try {
                    $testProcess.Kill()
                    $null = $testProcess.WaitForExit(5000)
                } catch {
                    # Ignore an already-exited race during fallback cleanup.
                }
            }
        }
        $testProcess.Close()
    }
}

# --- 5. Set environment variables ---------------------------------

Info "Setting environment variables..."

# Warn if env vars already point somewhere else (avoid silent clobber)
$oldRoot = [Environment]::GetEnvironmentVariable("LLM_WIKI_ROOT", "User")
if ($oldRoot -and $oldRoot -ne $VAULT_ROOT) {
    Warn "LLM_WIKI_ROOT was '$oldRoot', overwriting to '$VAULT_ROOT'"
}
$oldState = [Environment]::GetEnvironmentVariable("LLM_WIKI_STATE_ROOT", "User")
$processState = $env:LLM_WIKI_STATE_ROOT
$STATE_ROOT = Resolve-StateRoot `
    -ProcessState $processState `
    -UserState $oldState `
    -VaultRoot $VAULT_ROOT

[Environment]::SetEnvironmentVariable("LLM_WIKI_ROOT", $VAULT_ROOT, "User")
# Runtime lives inside the vault as gitignored cache/logs/run dirs.
# LLM_WIKI_STATE_ROOT defaults to the vault itself; only set it explicitly
# if you want runtime on a different disk.
if ([string]::IsNullOrWhiteSpace($processState) -and [string]::IsNullOrWhiteSpace($oldState)) {
    [Environment]::SetEnvironmentVariable("LLM_WIKI_STATE_ROOT", $VAULT_ROOT, "User")
}
$env:LLM_WIKI_ROOT = $VAULT_ROOT
$env:LLM_WIKI_STATE_ROOT = $STATE_ROOT

New-Item -ItemType Directory -Path "$STATE_ROOT\run" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\run\queue" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\logs" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\cache" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\cache\cognee" -Force | Out-Null
Ok "LLM_WIKI_ROOT set (User scope); runtime at $STATE_ROOT\{run,logs,cache} (gitignored)"

# --- 6. Register Task Scheduler -----------------------------------

Info "Registering Windows Task Scheduler..."
$pythonExe = (Get-Command python).Source
try {
    & ".\scripts\install-scheduled-tasks.ps1" 2>$null
    if ($LASTEXITCODE -eq 0) { Ok "Task Scheduler: nightly 03:00 + weekly Sun 04:00" }
    else { Warn "Task Scheduler registration failed - run scripts\install-scheduled-tasks.ps1 manually" }
} catch {
    Warn "Task Scheduler registration failed - run scripts\install-scheduled-tasks.ps1 manually"
}

# --- 7. Detect and wire up agents ---------------------------------

Info "Detecting agents..."
$agents = @()

# OpenCode - detect by process OR config dir (process may not be running at install time)
$openCodeConfig = "$env:USERPROFILE\.config\opencode"
$openCodePluginSrc = Join-Path $VAULT_ROOT "scripts\llm-wiki-memory-opencode.js"
if ((Get-Process "OpenCode*" -ErrorAction SilentlyContinue) -or (Test-Path $openCodeConfig) -or (Get-Command opencode -ErrorAction SilentlyContinue)) {
    $agents += "OpenCode"
    $pluginDir = Join-Path $openCodeConfig "plugins"
    New-Item -ItemType Directory -Path $pluginDir -Force | Out-Null
    $pluginDst = Join-Path $pluginDir "llm-wiki-memory.js"
    if (Test-Path $openCodePluginSrc) {
        Copy-Item -LiteralPath $openCodePluginSrc -Destination $pluginDst -Force
        Ok "OpenCode plugin installed -> $pluginDst"
        # Generate initial context file so the first session has context
        $ctxFile = Join-Path $STATE_ROOT "cache\session-context.md"
        try { & uv run python (Join-Path $VAULT_ROOT "scripts\session_start_context.py") --output-file $ctxFile 2>$null | Out-Null } catch {}
    } else {
        Warn "OpenCode detected but plugin source missing: $openCodePluginSrc"
    }
    $openCodeMcp = Join-Path $openCodeConfig "opencode.json"
    $openCodeEntryObject = [ordered]@{
        type = "local"
        command = @("uv", "run", "--directory", $VAULT_ROOT, "python", "scripts/mcp_server.py")
        enabled = $true
    }
    $openCodeConfigObject = [ordered]@{
        mcp = [ordered]@{ "llm-wiki" = $openCodeEntryObject }
    }
    $openCodeJson = $openCodeConfigObject | ConvertTo-Json -Depth 6 -Compress
    $openCodeMerge = ([ordered]@{ "llm-wiki" = $openCodeEntryObject } | ConvertTo-Json -Depth 5 -Compress)
    if (-not (Test-Path $openCodeMcp)) {
        Write-Utf8NoBom $openCodeMcp $openCodeJson
        Ok "OpenCode MCP config created -> $openCodeMcp"
    } else {
        $openCodeExisting = Get-Content -LiteralPath $openCodeMcp -Raw
        if ($openCodeExisting -notmatch '"llm-wiki"\s*:') {
            Warn 'Existing opencode.json found without llm-wiki; merge this under top-level "mcp":'
            Warn "  $openCodeMerge"
        }
    }
}

# Codex
if (Get-Command codex -ErrorAction SilentlyContinue) {
    $agents += "Codex"
    $codexConfig = Join-Path $env:USERPROFILE ".codex\config.toml"
    $codexDir = Split-Path $codexConfig -Parent
    New-Item -ItemType Directory -Path $codexDir -Force | Out-Null
    $codexMcpExit = Install-CodexMcp -VaultRoot $VAULT_ROOT -Config $codexConfig
    if ($codexMcpExit -eq 0) {
        Ok "Codex MCP config verified -> $codexConfig"
    } elseif ($codexMcpExit -eq 2) {
        Warn "Existing Codex MCP entry conflicts with LLM-Wiki; config.toml was not changed. Merge manually."
    } else {
        Warn "Codex MCP config could not be verified; config.toml was not changed."
    }
    $codexHooks = Join-Path $codexDir "hooks.json"
    $codexHookExit = Install-CodexHooks -VaultRoot $VAULT_ROOT -CodexDir $codexDir
    if ($codexHookExit -eq 0) {
        Ok "Codex official hooks merged -> $codexHooks"
        Info "Open /hooks in Codex to review and trust the LLM-Wiki commands."
    } elseif ($codexHookExit -eq 2) {
        Warn "Active inline Codex hooks require manual merge and /hooks trust review; hooks.json was not changed."
    } elseif ($codexHookExit -eq 3) {
        Ok "Equivalent LLM-Wiki hooks are already configured inline; hooks.json was not changed."
        Info "Open /hooks in Codex to review and trust the inline LLM-Wiki commands."
    } elseif ($codexHookExit -eq 4) {
        # Install-CodexHooks already printed the manual enable instruction.
    } else {
        Warn "Codex hooks were not changed; review the existing hooks configuration manually."
    }
    Info "The heartbeat-only codex-memory-wrapper is not installed automatically; official hooks are primary."
    Ok "Codex detected"
}

# Claude Code - merge hooks into user settings if CLI or config dir present
$claudeConfig = Join-Path $env:USERPROFILE ".claude"
$claudeUserConfig = Join-Path $env:USERPROFILE ".claude.json"
if ((Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path $claudeConfig) -or (Test-Path $claudeUserConfig)) {
    $agents += "Claude Code"
    Ok "Claude Code detected (or ~/.claude present)"
    Info "Merging LLM-wiki hooks into Claude user settings (backup first)..."
    uv run python (Join-Path $VAULT_ROOT "scripts\merge_claude_settings.py") `
        --vault-root $VAULT_ROOT `
        --state-root $STATE_ROOT 2>&1 | ForEach-Object { Info "$_" }
    if ($LASTEXITCODE -eq 0) {
        Ok "Claude settings merged -> $claudeConfig\settings.json"
    } else {
        Warn "Claude settings merge failed - run manually:"
        Warn "  uv run python scripts\merge_claude_settings.py"
    }
    $claudeMcp = $claudeUserConfig
    $claudeEntryObject = [ordered]@{
        command = "uv"
        args = @("run", "--directory", $VAULT_ROOT, "python", "scripts/mcp_server.py")
    }
    $claudeConfigObject = [ordered]@{
        mcpServers = [ordered]@{ "llm-wiki" = $claudeEntryObject }
    }
    $claudeJson = $claudeConfigObject | ConvertTo-Json -Depth 6 -Compress
    $claudeMerge = ([ordered]@{ "llm-wiki" = $claudeEntryObject } | ConvertTo-Json -Depth 5 -Compress)
    if (-not (Test-Path $claudeMcp)) {
        Write-Utf8NoBom $claudeMcp $claudeJson
        Ok "Claude MCP config created -> $claudeMcp"
    } else {
        $claudeExisting = Get-Content -LiteralPath $claudeMcp -Raw
        if ($claudeExisting -notmatch '"llm-wiki"\s*:') {
            Warn 'Existing ~/.claude.json found without llm-wiki; merge this under top-level "mcpServers":'
            Warn "  $claudeMerge"
        }
    }
}

# Cursor
if (Test-Path "$env:USERPROFILE\.cursor") {
    $agents += "Cursor"
    Ok "Cursor detected"
}

# Antigravity
if (Test-Path "$env:USERPROFILE\.antigravity" -ErrorAction SilentlyContinue) {
    $agents += "Antigravity"
    Ok "Antigravity detected - copy integrations\antigravity\AGENTS.md to your project root"
}

if ($agents.Count -eq 0) {
    Warn "No agents detected. Install OpenCode, Codex, Claude Code, Cursor, or Antigravity."
} else {
    Ok "Agents: $($agents -join ', ')"
}

# --- 8. Bounded runtime sync --------------------------------------

Info "Synchronizing runtime state and derived indexes..."
uv run --locked --no-sync python "$VAULT_ROOT\scripts\sync_memory.py" --apply
$syncExit = $LASTEXITCODE
$syncWarning = $false
switch ($syncExit) {
    0 { Ok "Runtime state synchronized" }
    1 { $syncWarning = $true; Warn "Runtime synchronization completed with warnings" }
    default { Fail "Runtime synchronization failed" }
}

# --- 9. Summary ---------------------------------------------------

Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
if ($syncWarning) {
    Write-Host "  LLM-Wiki installed with warnings" -ForegroundColor Yellow
} else {
    Write-Host "  LLM-Wiki installed successfully!" -ForegroundColor Green
}
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Vault:       $VAULT_ROOT"
Write-Host "State:       $VAULT_ROOT (cache/logs/run, gitignored)"
Write-Host "Agents:      $($agents -join ', ')"
Write-Host "Maintenance: Task Scheduler (nightly + weekly)"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Restart terminal"
Write-Host "  2. Open a project in your agent"
Write-Host "  3. Work normally - capture is automatic"
Write-Host ""
Write-Host "MCP baseline: 12 local task-shaped tools (installed)"
Write-Host "Optional enhancements:"
Write-Host "  uv sync --extra hybrid        # LanceDB HNSW + semantic search"
Write-Host "  uv sync --extra code-graph    # tree-sitter code graph"
Write-Host "  uv sync --extra reranker      # cross-encoder reranker"
Write-Host "  uv sync --extra full          # all of the above"
Write-Host ""
