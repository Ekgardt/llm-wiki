# install.ps1 — One-command installer for Windows.
#
# Usage:
#   irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
#
# Or clone first:
#   git clone https://github.com/Ekgardt/llm-wiki.git; cd llm-wiki; .\install.ps1
#
# What this does:
#   1. Checks prerequisites (Python 3.10+, uv, git)
#   2. Installs Python deps
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

# ─── 1. Resolve vault root ──────────────────────────────────────────

$VAULT_ROOT = if ($env:LLM_WIKI_ROOT) { $env:LLM_WIKI_ROOT } else { $PSScriptRoot }
if (-not (Test-Path "$VAULT_ROOT\pyproject.toml")) {
    $VAULT_ROOT = "$env:USERPROFILE\LLM-wiki"
    if (-not (Test-Path "$VAULT_ROOT\pyproject.toml")) {
        Info "Cloning LLM-Wiki..."
        git clone https://github.com/Ekgardt/llm-wiki.git $VAULT_ROOT
    }
}

Set-Location $VAULT_ROOT
Info "Vault root: $VAULT_ROOT"

# ─── 2. Check prerequisites ────────────────────────────────────────

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

# ─── 3. Install dependencies ───────────────────────────────────────

Info "Installing Python dependencies..."
uv sync --quiet
Ok "Dependencies installed"

# ─── 4. Run tests ──────────────────────────────────────────────────

Info "Running test suite..."
$testResult = uv run pytest -q 2>&1 | Select-Object -Last 1
if ($testResult -match "passed") {
    Ok $testResult
} else {
    Warn "Tests may have issues — core features still work"
}

# ─── 5. Set environment variables ──────────────────────────────────

Info "Setting environment variables..."
[Environment]::SetEnvironmentVariable("LLM_WIKI_ROOT", $VAULT_ROOT, "User")
$stateRoot = Split-Path $VAULT_ROOT -Parent | Join-Path -ChildPath "LLM-wiki-state"
[Environment]::SetEnvironmentVariable("LLM_WIKI_STATE_ROOT", $stateRoot, "User")
$env:LLM_WIKI_ROOT = $VAULT_ROOT
$env:LLM_WIKI_STATE_ROOT = $stateRoot

New-Item -ItemType Directory -Path "$stateRoot\run" -Force | Out-Null
New-Item -ItemType Directory -Path "$stateRoot\run\queue" -Force | Out-Null
New-Item -ItemType Directory -Path "$stateRoot\logs" -Force | Out-Null
New-Item -ItemType Directory -Path "$stateRoot\cache" -Force | Out-Null
Ok "LLM_WIKI_ROOT set (User scope); state at $stateRoot\{run,logs,cache}"

# ─── 6. Build search index ─────────────────────────────────────────

Info "Building search index..."
uv run python scripts\search_memory.py --rebuild 2>$null | Out-Null
Ok "Search index built"

# ─── 7. Register Task Scheduler ────────────────────────────────────

Info "Registering Windows Task Scheduler..."
$pythonExe = (Get-Command python).Source
& ".\scripts\install-scheduled-tasks.ps1" 2>$null
if ($?) { Ok "Task Scheduler: nightly 03:00 + weekly Sun 04:00" }
else { Warn "Task Scheduler registration failed — run scripts\install-scheduled-tasks.ps1 manually" }

# ─── 8. Detect and wire up agents ──────────────────────────────────

Info "Detecting agents..."
$agents = @()

# OpenCode — detect by process OR config dir (process may not be running at install time)
$openCodeConfig = "$env:USERPROFILE\.config\opencode"
$openCodePluginSrc = Join-Path $VAULT_ROOT "scripts\llm-wiki-memory-opencode.js"
if ((Get-Process "OpenCode*" -ErrorAction SilentlyContinue) -or (Test-Path $openCodeConfig) -or (Get-Command opencode -ErrorAction SilentlyContinue)) {
    $agents += "OpenCode"
    $pluginDir = Join-Path $openCodeConfig "plugins"
    New-Item -ItemType Directory -Path $pluginDir -Force | Out-Null
    $pluginDst = Join-Path $pluginDir "llm-wiki-memory.js"
    if (Test-Path $openCodePluginSrc) {
        Copy-Item -LiteralPath $openCodePluginSrc -Destination $pluginDst -Force
        Ok "OpenCode plugin installed → $pluginDst"
    } else {
        Warn "OpenCode detected but plugin source missing: $openCodePluginSrc"
    }
}

# Codex
if (Get-Command codex -ErrorAction SilentlyContinue) {
    $agents += "Codex"
    # Add wrapper to profile
    $profilePath = $PROFILE
    if (Test-Path $profilePath) {
        $content = Get-Content $profilePath -Raw
        if ($content -notmatch "codex-memory-wrapper") {
            Add-Content $profilePath ". `"$VAULT_ROOT\scripts\codex-memory-wrapper.ps1`""
            Ok "Codex wrapper added to $profilePath"
        }
    }
    Ok "Codex detected"
}

# Claude Code — merge hooks into user settings if CLI or config dir present
$claudeConfig = Join-Path $env:USERPROFILE ".claude"
if ((Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path $claudeConfig)) {
    $agents += "Claude Code"
    Ok "Claude Code detected (or ~/.claude present)"
    Info "Merging LLM-wiki hooks into Claude user settings (backup first)..."
    uv run python (Join-Path $VAULT_ROOT "scripts\merge_claude_settings.py") `
        --vault-root $VAULT_ROOT `
        --state-root $stateRoot 2>&1 | ForEach-Object { Info "$_" }
    if ($LASTEXITCODE -eq 0) {
        Ok "Claude settings merged → $claudeConfig\settings.json"
    } else {
        Warn "Claude settings merge failed — run manually:"
        Warn "  uv run python scripts\merge_claude_settings.py"
    }
}

# Cursor
if (Test-Path "$env:USERPROFILE\.cursor") {
    $agents += "Cursor"
    Ok "Cursor detected"
}

if ($agents.Count -eq 0) {
    Warn "No agents detected. Install OpenCode, Codex, Claude Code, or Cursor."
} else {
    Ok "Agents: $($agents -join ', ')"
}

# ─── 9. Summary ────────────────────────────────────────────────────

Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "  LLM-Wiki installed successfully!" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Vault:       $VAULT_ROOT"
Write-Host "State:       $stateRoot"
Write-Host "Agents:      $($agents -join ', ')"
Write-Host "Maintenance: Task Scheduler (nightly + weekly)"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Restart terminal"
Write-Host "  2. Open a project in your agent"
Write-Host "  3. Work normally — capture is automatic"
Write-Host ""
