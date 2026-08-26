# install.ps1 - One-command installer for Windows.
#
# Usage from an inspected checkout:
#   git clone https://github.com/Ekgardt/llm-wiki.git
#   cd llm-wiki
#   $env:LLM_WIKI_ROOT = (Get-Location).Path
#   .\install.ps1
#
# Remote bootstrap requires LLM_WIKI_COMMIT to be a full commit OID.
#
# What this does:
#   1. Checks prerequisites (Python 3.10+, uv, git)
#   2. Installs the locked production dependency baseline
#   3. Runs a bounded production smoke
#   4. Sets LLM_WIKI_ROOT environment variable (user-level)
#   5. Registers Windows Task Scheduler (nightly + weekly)
#   6. Detects agents (Claude Code, OpenCode, Codex)
#   7. Wires up Codex wrapper to PowerShell profile
#   8. Copies OpenCode plugin if OpenCode is installed
#   9. Builds search index
#
# Safe to re-run. Idempotent.

[CmdletBinding()]
param(
    [switch]$ProtectPush
)

$ErrorActionPreference = "Stop"
$UvVersion = "0.12.3"

function Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Blue }
function Ok($msg)   { Write-Host "[OK] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "[WARN] $msg"  -ForegroundColor Yellow }
function Fail($msg) { Write-Host "[FAIL] $msg"  -ForegroundColor Red; exit 1 }
function Invoke-NativeCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [int[]]$AllowedExitCodes = @(0),
        [switch]$CaptureOutput,
        [switch]$ReturnResult
    )
    if ($CaptureOutput) {
        $output = @(& $FilePath @ArgumentList)
    } else {
        & $FilePath @ArgumentList
    }
    $nativeExit = $LASTEXITCODE
    if ($AllowedExitCodes -notcontains $nativeExit) {
        throw "$FilePath failed with exit code $nativeExit"
    }
    if ($ReturnResult) {
        return [pscustomobject]@{
            ExitCode = $nativeExit
            Output = if ($CaptureOutput) { $output -join [Environment]::NewLine } else { $null }
        }
    }
    if ($CaptureOutput) { return ($output -join [Environment]::NewLine) }
}
function Protect-PushUrls([string]$VaultRoot) {
    $remoteResult = Invoke-NativeCommand git @("-C", $VaultRoot, "remote") -CaptureOutput -ReturnResult
    $remotes = @($remoteResult.Output -split "`r?`n" | Where-Object { $_ })
    foreach ($remote in $remotes) {
        $key = "remote.$remote.pushurl"
        $probe = Invoke-NativeCommand git @("-C", $VaultRoot, "config", "--get-all", $key) `
            -AllowedExitCodes @(0, 1) -CaptureOutput -ReturnResult
        if ($probe.ExitCode -eq 0) {
            Invoke-NativeCommand git @("-C", $VaultRoot, "config", "--unset-all", $key)
        }
        Invoke-NativeCommand git @("-C", $VaultRoot, "config", "--add", $key, "no-push")
        $verify = Invoke-NativeCommand git @("-C", $VaultRoot, "remote", "get-url", "--all", "--push", $remote) `
            -CaptureOutput -ReturnResult
        $urls = @($verify.Output -split "`r?`n" | Where-Object { $_ })
        if ($urls.Count -ne 1 -or $urls[0] -ne "no-push") {
            throw "Could not protect push URLs for remote $remote"
        }
    }
}
function Protect-PushUrlsIfAuthorized(
    [string]$VaultRoot,
    [bool]$InstallerCreatedClone,
    [bool]$ProtectPush
) {
    if ($InstallerCreatedClone -or $ProtectPush) {
        Protect-PushUrls -VaultRoot $VaultRoot
    }
}
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
function Get-CodexInlineHooksState(
    [string]$VaultRoot,
    [string]$CodexDir
) {
    # Asked before the ownership transaction, and it writes nothing: the
    # transaction may own hooks.json only when the inline configuration neither
    # disables the feature, already carries our handlers, nor contradicts them.
    $state = (& uv run --directory $VaultRoot python (Join-Path $VaultRoot "scripts\codex_memory.py") `
        hooks-state `
        --source (Join-Path $VaultRoot "integrations\codex\hooks.json") `
        --config (Join-Path $CodexDir "config.toml") | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $state) { return "unknown" }
    return $state
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
args = ["run", "--locked", "--no-sync", "--directory", "$tomlVault", "python", "scripts/mcp_server.py"]
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

$repositoryUrl = "https://github.com/Ekgardt/llm-wiki.git"
$callerDirectory = (Get-Location).Path
$installerCreatedClone = $env:LLM_WIKI_INSTALLER_CREATED_CLONE -eq "1"
$scriptDirectory = if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) { $null } else { $PSScriptRoot }

if ($scriptDirectory -and (Test-Path -LiteralPath (Join-Path $scriptDirectory "pyproject.toml"))) {
    $VAULT_ROOT = if ($env:LLM_WIKI_ROOT) { $env:LLM_WIKI_ROOT } else { $scriptDirectory }
    $resolvedScriptRoot = (Resolve-Path -LiteralPath $scriptDirectory).Path
    try {
        $requestedRoot = (Resolve-Path -LiteralPath $VAULT_ROOT).Path
    } catch {
        Fail "LLM_WIKI_ROOT does not identify an accessible checkout."
    }
    if (-not [string]::Equals($requestedRoot, $resolvedScriptRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "LLM_WIKI_ROOT points to a different checkout than this installer."
    }
    $VAULT_ROOT = $resolvedScriptRoot
} else {
    if ($env:LLM_WIKI_COMMIT -notmatch '^[0-9a-fA-F]{40}$') {
        Fail "Remote bootstrap requires LLM_WIKI_COMMIT as a full 40-hex commit OID"
    }
    $commit = $env:LLM_WIKI_COMMIT.ToLowerInvariant()
    $VAULT_ROOT = Join-Path $env:USERPROFILE "LLM-wiki"
    if (Test-Path -LiteralPath $VAULT_ROOT) { Fail "Remote install target already exists: $VAULT_ROOT" }
    Invoke-NativeCommand git @("init", $VAULT_ROOT)
    Invoke-NativeCommand git @("-C", $VAULT_ROOT, "remote", "add", "origin", $repositoryUrl)
    Invoke-NativeCommand git @("-C", $VAULT_ROOT, "fetch", "--depth", "1", "origin", $commit)
    Invoke-NativeCommand git @("-C", $VAULT_ROOT, "checkout", "--detach", $commit)
    $installerCreatedClone = $true
    $head = (Invoke-NativeCommand git @("-C", $VAULT_ROOT, "rev-parse", "HEAD") -CaptureOutput).Trim()
    if ($head -ne $commit) { Fail "Checked-out commit does not match LLM_WIKI_COMMIT" }
    $origin = (Invoke-NativeCommand git @("-C", $VAULT_ROOT, "remote", "get-url", "origin") -CaptureOutput).Trim()
    if ($origin -ne $repositoryUrl) { Fail "Installed checkout repository identity does not match LLM-Wiki" }
    foreach ($required in @("pyproject.toml", "uv.lock", "install.sh", "install.ps1", "scripts\installer_config.py", "scripts\install_control.py")) {
        if (-not (Test-Path -LiteralPath (Join-Path $VAULT_ROOT $required) -PathType Leaf)) {
            Fail "Installed checkout is missing $($required.Replace('\', '/'))"
        }
    }
    $env:LLM_WIKI_ROOT = $VAULT_ROOT
    $env:LLM_WIKI_INSTALLER_CREATED_CLONE = "1"
    $hostExecutable = if ($PSVersionTable.PSEdition -eq "Core" -and $PSVersionTable.Platform -eq "Unix") {
        Join-Path $PSHOME "pwsh"
    } elseif ($PSVersionTable.PSEdition -eq "Core") {
        Join-Path $PSHOME "pwsh.exe"
    } else {
        Join-Path $PSHOME "powershell.exe"
    }
    $reexecArguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $VAULT_ROOT "install.ps1")
    )
    if ($ProtectPush) { $reexecArguments += "-ProtectPush" }
    try {
        & $hostExecutable @reexecArguments
        $nativeExit = $LASTEXITCODE
    } finally {
        Remove-Item Env:LLM_WIKI_INSTALLER_CREATED_CLONE -ErrorAction SilentlyContinue
    }
    if ($nativeExit -ne 0) { throw "Checked-out installer failed with exit code $nativeExit" }
    exit 0
}

$VAULT_ROOT = [System.IO.Path]::GetFullPath($VAULT_ROOT)
$userState = [Environment]::GetEnvironmentVariable("LLM_WIKI_STATE_ROOT", "User")
$stateInput = Resolve-StateRoot `
    -ProcessState $env:LLM_WIKI_STATE_ROOT `
    -UserState $userState `
    -VaultRoot $VAULT_ROOT
$stateInput = [System.IO.Path]::GetFullPath($stateInput)
New-Item -ItemType Directory -Path $stateInput -Force | Out-Null
$STATE_ROOT = (Resolve-Path -LiteralPath $stateInput).Path
$env:LLM_WIKI_ROOT = $VAULT_ROOT
$env:LLM_WIKI_STATE_ROOT = $STATE_ROOT

Set-Location $VAULT_ROOT
Info "Vault root: $VAULT_ROOT"
Info "State root: $STATE_ROOT"

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

# uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "uv is required at version $UvVersion. Install it from https://astral.sh/uv/$UvVersion/install.ps1 and rerun the installer."
}
$installedUvVersion = ((& uv --version) -split '\s+')[1]
if ($LASTEXITCODE -ne 0 -or $installedUvVersion -ne $UvVersion) {
    Fail "uv is required at version $UvVersion, found $installedUvVersion. Upgrade uv explicitly and rerun the installer."
}
Ok "uv $installedUvVersion"

# --- 3. Install dependencies --------------------------------------

Info "Installing locked production dependencies..."
$syncPlanJson = Invoke-NativeCommand python @(
    (Join-Path $VAULT_ROOT "scripts\installer_config.py"),
    "sync-args", "--root", $VAULT_ROOT, "--environment", [string]$env:UV_PROJECT_ENVIRONMENT
) -CaptureOutput
$syncPlan = $syncPlanJson | ConvertFrom-Json
$env:UV_PROJECT_ENVIRONMENT = $syncPlan.environment
Invoke-NativeCommand uv @($syncPlan.arguments)
Ok "Production dependencies installed (MCP included)"

# --- 4. Run production smoke --------------------------------------

Info "Running production smoke..."
$testTimeoutSeconds = 180
$configuredTestTimeout = $env:LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS
if (-not [string]::IsNullOrWhiteSpace($configuredTestTimeout)) {
    if (-not [int]::TryParse($configuredTestTimeout, [ref]$testTimeoutSeconds) -or $testTimeoutSeconds -le 0) {
        Fail "LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS must be a positive integer"
    }
}
$testTimeoutMilliseconds = [int]($testTimeoutSeconds * 1000)
$testProcess = $null
$testFailure = $null
try {
    $testProcess = Start-Process `
        -FilePath "uv" `
        -ArgumentList "run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120" `
        -NoNewWindow `
        -PassThru
    # Windows PowerShell 5.1 needs an open handle to retain a fast process's exit code.
    $null = $testProcess.Handle
    if (-not $testProcess.WaitForExit($testTimeoutMilliseconds)) {
        $testFailure = "Production smoke timed out after ${testTimeoutSeconds}s; installation aborted"
    } else {
        $testExit = $testProcess.ExitCode
        if ($testExit -ne 0) {
            $testFailure = "Production smoke failed; installation aborted"
        } else {
            Ok "Production smoke passed"
        }
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
if ($null -ne $testFailure) { Fail $testFailure }

# --- 5. Set environment variables ---------------------------------

# External configuration starts only after the mandatory test gate.
Protect-PushUrlsIfAuthorized `
    -VaultRoot $VAULT_ROOT `
    -InstallerCreatedClone $installerCreatedClone `
    -ProtectPush ([bool]$ProtectPush)
if ($installerCreatedClone -or $ProtectPush) {
    Ok "Push disabled (no-push) for every configured remote"
}

Info "Setting environment variables..."

New-Item -ItemType Directory -Path "$STATE_ROOT\run" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\run\queue" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\logs" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\cache" -Force | Out-Null
Ok "LLM_WIKI_ROOT set (User scope); runtime at $STATE_ROOT\{run,logs,cache} (gitignored)"

# --- 6. Register Task Scheduler -----------------------------------

Info "Registering Windows Task Scheduler..."
$uvPath = (Get-Command uv).Source
$schedulerWarning = $false
# Detected here, before the transaction, so the settings our hooks live in are
# owned by it: an uninstall has to take back exactly what the install wrote.
$claudeDetected = [bool]((Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path "$env:USERPROFILE\.claude") -or (Test-Path "$env:USERPROFILE\.claude.json"))
$codexDetected = [bool]((Get-Command codex -ErrorAction SilentlyContinue) -or (Test-Path "$env:USERPROFILE\.codex"))
$codexHooksState = "none"
if ($codexDetected) {
    New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE ".codex") | Out-Null
    $codexHooksState = Get-CodexInlineHooksState -VaultRoot $VAULT_ROOT -CodexDir (Join-Path $env:USERPROFILE ".codex")
}
try {
    $powerShellPath = [System.Diagnostics.Process]::GetCurrentProcess().Path
    $installControlArgs = @(
        "run", "--locked", "--no-sync", "--directory", $VAULT_ROOT,
        "python", (Join-Path $VAULT_ROOT "scripts\install_control.py"),
        "install", "--root", $VAULT_ROOT, "--state-root", $STATE_ROOT,
        "--uv-path", $uvPath, "--home", $env:USERPROFILE,
        "--scheduler", "native",
        "--powershell-path", $powerShellPath
    )
    if ($claudeDetected) { $installControlArgs += "--claude-settings" }
    if ($codexHooksState -eq "absent") { $installControlArgs += "--codex-hooks" }
    $installControlJson = Invoke-NativeCommand uv $installControlArgs -CaptureOutput
    $installControl = $installControlJson | ConvertFrom-Json
    if ($installControl.status -ne "committed" -or
        $installControl.scheduler_backend -ne "task_scheduler") {
        throw "Install control returned an invalid result"
    }
    Ok "Task Scheduler verified: nightly 03:00 + weekly Sun 04:00 (Interactive)"
} catch {
    $schedulerWarning = $true
    Warn "Install ownership transaction or Task Scheduler verification failed"
}

# --- 7. Detect and wire up agents ---------------------------------

Info "Detecting agents..."
$agents = @()

# OpenCode configuration is merged structurally and verified after all precedence layers.
$openCodeResult = Invoke-NativeCommand uv @(
    "run", "--locked", "--no-sync", "--directory", $VAULT_ROOT,
    "python", (Join-Path $VAULT_ROOT "scripts\installer_config.py"),
    "opencode", "--root", $VAULT_ROOT, "--state-root", $STATE_ROOT,
    "--cwd", $callerDirectory
) -CaptureOutput -ReturnResult
$openCode = $openCodeResult.Output | ConvertFrom-Json
switch ($openCode.status) {
    "active" {
        $agents += "OpenCode: active automatic"
        Ok "OpenCode configuration is active"
        $ctxFile = Join-Path $STATE_ROOT "cache\session-context.md"
        & uv run --locked --no-sync --directory $VAULT_ROOT python `
            (Join-Path $VAULT_ROOT "scripts\session_start_context.py") `
            --output-file $ctxFile 2>$null | Out-Null
    }
    "conflict" {
        $agents += "OpenCode: conflict"
        Warn "OpenCode configuration status: conflict"
    }
    "configured_unverified" {
        $agents += "OpenCode: configured unverified"
        Warn "OpenCode configuration status: configured_unverified"
    }
    "not_detected" { }
    default { Fail "OpenCode configuration helper returned an invalid status" }
}

# Codex
if (Get-Command codex -ErrorAction SilentlyContinue) {
    $codexMcpReady = $false
    $codexHooksReady = $false
    $codexConfig = Join-Path $env:USERPROFILE ".codex\config.toml"
    $codexDir = Split-Path $codexConfig -Parent
    New-Item -ItemType Directory -Path $codexDir -Force | Out-Null
    $codexMcpExit = Install-CodexMcp -VaultRoot $VAULT_ROOT -Config $codexConfig
    if ($codexMcpExit -eq 0) {
        $codexMcpReady = $true
        Ok "Codex MCP config verified -> $codexConfig"
    } elseif ($codexMcpExit -eq 2) {
        Warn "Existing Codex MCP entry conflicts with LLM-Wiki; config.toml was not changed. Merge manually."
    } else {
        Warn "Codex MCP config could not be verified; config.toml was not changed."
    }
    $codexHooks = Join-Path $codexDir "hooks.json"
    if ($codexHooksState -eq "absent") {
        $codexHooksReady = $true
        Ok "Codex official hooks owned by the install transaction -> $codexHooks"
        Info "Open /hooks in Codex to review and trust the LLM-Wiki commands."
    } elseif ($codexHooksState -eq "equivalent") {
        $codexHooksReady = $true
        Ok "Equivalent LLM-Wiki hooks are already configured inline; hooks.json was not changed."
        Info "Open /hooks in Codex to review and trust the inline LLM-Wiki commands."
    } elseif ($codexHooksState -eq "conflict") {
        Warn "Active inline Codex hooks require manual merge and /hooks trust review; hooks.json was not changed."
    } elseif ($codexHooksState -eq "disabled") {
        Warn "Codex lifecycle hooks are disabled. Set [features] hooks = true in config.toml and rerun the installer; hooks.json was not changed."
    } else {
        Warn "Codex hooks were not changed; review the existing hooks configuration manually."
    }
    Info "The heartbeat-only codex-memory-wrapper is not installed automatically; official hooks are primary."
    if ($codexMcpReady -and $codexHooksReady) {
        $agents += "Codex: manual /hooks trust review required"
    } else {
        $agents += "Codex: conflict or unverified"
    }
}

# Claude Code - hooks and env are owned by the install transaction (step 6)
$claudeConfig = Join-Path $env:USERPROFILE ".claude"
$claudeUserConfig = Join-Path $env:USERPROFILE ".claude.json"
if ($claudeDetected) {
    $claudeAutomatic = $true
    Ok "Claude settings owned by the install transaction -> $claudeConfig\settings.json"
    $claudeMcp = $claudeUserConfig
    $claudeEntryObject = [ordered]@{
        command = "uv"
        args = @("run", "--locked", "--no-sync", "--directory", $VAULT_ROOT, "python", "scripts/mcp_server.py")
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
    if ($claudeAutomatic) {
        $agents += "Claude Code: active automatic"
    } else {
        $agents += "Claude Code: conflict or unverified"
    }
}

if ($agents.Count -eq 0) {
    Warn "No agents detected. Install Claude Code, OpenCode, or Codex."
} else {
    Ok "Agent integrations:"
    $agents | ForEach-Object { Info "  - $_" }
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
if ($syncWarning -or $schedulerWarning) {
    Write-Host "  LLM-Wiki installed with warnings" -ForegroundColor Yellow
} else {
    Write-Host "  LLM-Wiki installed successfully!" -ForegroundColor Green
}
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Vault:       $VAULT_ROOT"
Write-Host "State:       $STATE_ROOT (cache/logs/run, gitignored)"
Write-Host "Agent integrations:"
if ($agents.Count -eq 0) {
    Write-Host "  - none detected"
} else {
    $agents | ForEach-Object { Write-Host "  - $_" }
}
if ($schedulerWarning) {
    Write-Host "Maintenance: not registered" -ForegroundColor Yellow
} else {
    Write-Host "Maintenance: Task Scheduler (nightly + weekly; logged-on user only)"
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Restart terminal"
Write-Host "  2. Open a project in your agent"
Write-Host "  3. Review the integration states above; automatic capture runs only for active automatic entries"
Write-Host ""
Write-Host "MCP baseline: 12 local task-shaped tools (installed)"
Write-Host "Optional enhancements:"
Write-Host "  uv sync --locked --no-default-groups --inexact --extra hybrid"
Write-Host "  uv sync --locked --no-default-groups --inexact --extra code-graph"
Write-Host "  uv sync --locked --no-default-groups --inexact --extra reranker"
Write-Host ""
if ($schedulerWarning) { exit 1 }
