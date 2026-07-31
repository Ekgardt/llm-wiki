# install.ps1 — One-command installer for Windows.
#
# Usage:
#   irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
#
# Or clone first:
#   git clone https://github.com/Ekgardt/llm-wiki.git; cd llm-wiki; .\install.ps1
#
# NOTE: For reproducible installs, pin to a version tag:
#   irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/v3.4.0/install.ps1 | iex
# The main branch URL is for development convenience only.
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

function Test-LlmWikiCheckout($root) {
    if ([string]::IsNullOrWhiteSpace($root)) { return $false }
    try {
        return Test-Path -LiteralPath (Join-Path $root "pyproject.toml") -PathType Leaf
    } catch {
        return $false
    }
}

function Get-OpenCodeConfigs {
    $fallbackConfigHome = [System.IO.Path]::GetFullPath(
        (Join-Path $env:USERPROFILE ".config")
    )
    $effectiveOpenCodeConfig = Join-Path $fallbackConfigHome "opencode"
    if (
        -not [string]::IsNullOrWhiteSpace($env:XDG_CONFIG_HOME) -and
        [System.IO.Path]::IsPathFullyQualified($env:XDG_CONFIG_HOME)
    ) {
        $effectiveOpenCodeConfig = Join-Path $env:XDG_CONFIG_HOME "opencode"
    }
    $windowsOpenCodeConfig = Join-Path $fallbackConfigHome "opencode"
    $openCodeConfigSet = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    $openCodeConfigs = @()
    $candidateOpenCodeConfig = $null
    foreach ($candidateOpenCodeConfig in @(
        $effectiveOpenCodeConfig,
        $windowsOpenCodeConfig
    )) {
        $normalizedOpenCodeConfig = [System.IO.Path]::GetFullPath($candidateOpenCodeConfig)
        if ($openCodeConfigSet.Add($normalizedOpenCodeConfig)) {
            $openCodeConfigs += $normalizedOpenCodeConfig
        }
    }
    return $openCodeConfigs
}

function Update-PowerShellProfile(
    [string]$ProfilePath,
    [string]$WrapperLine = '. "$env:LLM_WIKI_ROOT\scripts\codex-memory-wrapper.ps1"'
) {
    if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
        throw "PowerShell profile path is empty"
    }
    if (
        [string]::IsNullOrWhiteSpace($WrapperLine) -or
        $WrapperLine.Contains("`r") -or
        $WrapperLine.Contains("`n")
    ) {
        throw "PowerShell profile wrapper must be one non-empty line"
    }

    $requestedPath = [System.IO.Path]::GetFullPath($ProfilePath)
    $requestedItem = $null
    try {
        $requestedItem = Get-Item -LiteralPath $requestedPath -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        $requestedItem = $null
    }

    $requestedWasLink = $false
    $targetPath = $requestedPath
    $resolvedTarget = $null
    if ($null -ne $requestedItem) {
        if ($requestedItem.PSIsContainer) {
            throw "PowerShell profile path is a directory: $requestedPath"
        }
        if (
            ($requestedItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            $requestedWasLink = $true
            if ($null -eq $requestedItem.PSObject.Methods["ResolveLinkTarget"]) {
                throw "PowerShell profile symlink cannot be resolved safely: $requestedPath"
            }
            $resolvedTarget = $requestedItem.ResolveLinkTarget($true)
            if ($null -eq $resolvedTarget -or $resolvedTarget.PSIsContainer) {
                throw "PowerShell profile symlink must resolve to a file: $requestedPath"
            }
            $targetPath = [System.IO.Path]::GetFullPath($resolvedTarget.FullName)
        } else {
            $targetPath = [System.IO.Path]::GetFullPath($requestedItem.FullName)
        }
    }

    $targetItem = $null
    $originalBytes = [byte[]]@()
    if ($null -ne $requestedItem) {
        $targetItem = Get-Item -LiteralPath $targetPath -Force -ErrorAction Stop
        if (
            $targetItem.PSIsContainer -or
            ($targetItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            throw "PowerShell profile target must be a regular file: $targetPath"
        }
        $originalBytes = [System.IO.File]::ReadAllBytes($targetPath)
    }

    $bomLength = 0
    $encoding = [System.Text.Encoding]::GetEncoding(28591)
    if (
        $originalBytes.Length -ge 4 -and
        $originalBytes[0] -eq 0x00 -and $originalBytes[1] -eq 0x00 -and
        $originalBytes[2] -eq 0xFE -and $originalBytes[3] -eq 0xFF
    ) {
        $bomLength = 4
        $encoding = [System.Text.UTF32Encoding]::new($true, $false, $true)
    } elseif (
        $originalBytes.Length -ge 4 -and
        $originalBytes[0] -eq 0xFF -and $originalBytes[1] -eq 0xFE -and
        $originalBytes[2] -eq 0x00 -and $originalBytes[3] -eq 0x00
    ) {
        $bomLength = 4
        $encoding = [System.Text.UTF32Encoding]::new($false, $false, $true)
    } elseif (
        $originalBytes.Length -ge 3 -and
        $originalBytes[0] -eq 0xEF -and $originalBytes[1] -eq 0xBB -and
        $originalBytes[2] -eq 0xBF
    ) {
        $bomLength = 3
        $encoding = [System.Text.UTF8Encoding]::new($false, $true)
    } elseif (
        $originalBytes.Length -ge 2 -and
        $originalBytes[0] -eq 0xFF -and $originalBytes[1] -eq 0xFE
    ) {
        $bomLength = 2
        $encoding = [System.Text.UnicodeEncoding]::new($false, $false, $true)
    } elseif (
        $originalBytes.Length -ge 2 -and
        $originalBytes[0] -eq 0xFE -and $originalBytes[1] -eq 0xFF
    ) {
        $bomLength = 2
        $encoding = [System.Text.UnicodeEncoding]::new($true, $false, $true)
    }

    $profileText = $encoding.GetString(
        $originalBytes,
        $bomLength,
        $originalBytes.Length - $bomLength
    )
    $profileLines = [System.Text.RegularExpressions.Regex]::Split(
        $profileText,
        "\r\n|\n|\r"
    )
    $profileLine = $null
    foreach ($profileLine in $profileLines) {
        if ([string]::Equals($profileLine, $WrapperLine, [System.StringComparison]::Ordinal)) {
            return $false
        }
    }

    $newline = [System.Environment]::NewLine
    if ($profileText.Contains("`r`n")) {
        $newline = "`r`n"
    } elseif ($profileText.Contains("`n")) {
        $newline = "`n"
    } elseif ($profileText.Contains("`r")) {
        $newline = "`r"
    }
    if ($profileText.Length -eq 0) {
        $suffix = $WrapperLine + $newline
    } elseif ($profileText.EndsWith("`r") -or $profileText.EndsWith("`n")) {
        $suffix = $WrapperLine + $newline
    } else {
        $suffix = $newline + $WrapperLine + $newline
    }
    $appendBytes = $encoding.GetBytes($suffix)
    $updatedBytes = [byte[]]::new($originalBytes.Length + $appendBytes.Length)
    [System.Buffer]::BlockCopy(
        $originalBytes,
        0,
        $updatedBytes,
        0,
        $originalBytes.Length
    )
    [System.Buffer]::BlockCopy(
        $appendBytes,
        0,
        $updatedBytes,
        $originalBytes.Length,
        $appendBytes.Length
    )

    $profileDirectory = [System.IO.Path]::GetDirectoryName($targetPath)
    if (-not (Test-Path -LiteralPath $profileDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $profileDirectory -Force | Out-Null
    }
    $temporaryName = (
        "." + [System.IO.Path]::GetFileName($targetPath) + ".llm-wiki." +
        [System.Guid]::NewGuid().ToString("N") + ".tmp"
    )
    $temporaryPath = Join-Path $profileDirectory $temporaryName
    $stream = $null
    try {
        $stream = [System.IO.FileStream]::new(
            $temporaryPath,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None,
            4096,
            [System.IO.FileOptions]::WriteThrough
        )
        $stream.Write($updatedBytes, 0, $updatedBytes.Length)
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null

        if ($null -ne $targetItem) {
            $targetAttributes = [System.IO.File]::GetAttributes($targetPath)
            $temporaryAttributes = $targetAttributes -band (
                -bnot (
                    [System.IO.FileAttributes]::ReadOnly -bor
                    [System.IO.FileAttributes]::ReparsePoint
                )
            )
            if ($temporaryAttributes -eq 0) {
                $temporaryAttributes = [System.IO.FileAttributes]::Normal
            }
            [System.IO.File]::SetAttributes($temporaryPath, $temporaryAttributes)
        }

        $pathComparison = [System.StringComparison]::Ordinal
        if ($env:OS -eq "Windows_NT") {
            $pathComparison = [System.StringComparison]::OrdinalIgnoreCase
        }
        if ($requestedWasLink) {
            $currentRequested = Get-Item -LiteralPath $requestedPath -Force -ErrorAction Stop
            if (
                ($currentRequested.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0 -or
                $null -eq $currentRequested.PSObject.Methods["ResolveLinkTarget"]
            ) {
                throw "PowerShell profile symlink changed before publication"
            }
            $currentResolved = $currentRequested.ResolveLinkTarget($true)
            if (
                $null -eq $currentResolved -or
                -not [string]::Equals(
                    [System.IO.Path]::GetFullPath($currentResolved.FullName),
                    $targetPath,
                    $pathComparison
                )
            ) {
                throw "PowerShell profile symlink changed before publication"
            }
        } elseif ($null -ne $requestedItem) {
            $currentRequested = Get-Item -LiteralPath $requestedPath -Force -ErrorAction Stop
            if (
                ($currentRequested.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
                -not [string]::Equals(
                    [System.IO.Path]::GetFullPath($currentRequested.FullName),
                    $targetPath,
                    $pathComparison
                )
            ) {
                throw "PowerShell profile target changed before publication"
            }
        } elseif (
            [System.IO.File]::Exists($requestedPath) -or
            [System.IO.Directory]::Exists($requestedPath)
        ) {
            throw "PowerShell profile target appeared before publication"
        }

        if ($null -ne $targetItem) {
            $currentTarget = Get-Item -LiteralPath $targetPath -Force -ErrorAction Stop
            if (
                $currentTarget.PSIsContainer -or
                ($currentTarget.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                throw "PowerShell profile target changed before publication"
            }
            $currentBytes = [System.IO.File]::ReadAllBytes($targetPath)
            if ($currentBytes.Length -ne $originalBytes.Length) {
                throw "PowerShell profile content changed before publication"
            }
            $byteIndex = 0
            for ($byteIndex = 0; $byteIndex -lt $currentBytes.Length; $byteIndex++) {
                if ($currentBytes[$byteIndex] -ne $originalBytes[$byteIndex]) {
                    throw "PowerShell profile content changed before publication"
                }
            }
            [System.IO.File]::Replace(
                $temporaryPath,
                $targetPath,
                [System.Management.Automation.Language.NullString]::Value,
                $false
            )
        } else {
            [System.IO.File]::Move($temporaryPath, $targetPath)
        }
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
        if ([System.IO.File]::Exists($temporaryPath)) {
            $cleanupAttributes = [System.IO.File]::GetAttributes($temporaryPath)
            if (
                ($cleanupAttributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0
            ) {
                $cleanupAttributes = $cleanupAttributes -band (
                    -bnot [System.IO.FileAttributes]::ReadOnly
                )
                if ($cleanupAttributes -eq 0) {
                    $cleanupAttributes = [System.IO.FileAttributes]::Normal
                }
                [System.IO.File]::SetAttributes($temporaryPath, $cleanupAttributes)
            }
            [System.IO.File]::Delete($temporaryPath)
        }
    }
    return $true
}

# ─── 1. Resolve vault root ──────────────────────────────────────────

$VAULT_ROOT = if (Test-LlmWikiCheckout $PSScriptRoot) {
    [System.IO.Path]::GetFullPath($PSScriptRoot)
} elseif (Test-LlmWikiCheckout $env:LLM_WIKI_ROOT) {
    [System.IO.Path]::GetFullPath($env:LLM_WIKI_ROOT)
} else {
    Join-Path $env:USERPROFILE "LLM-wiki"
}
if (-not (Test-LlmWikiCheckout $VAULT_ROOT)) {
    Info "Cloning LLM-Wiki..."
    git clone --branch v3.4.0 --depth 1 https://github.com/Ekgardt/llm-wiki.git $VAULT_ROOT
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
uv sync --locked --quiet
if ($LASTEXITCODE -ne 0) {
    Fail "Dependency installation failed"
}
Ok "Dependencies installed"

# ─── 4. Run tests ──────────────────────────────────────────────────

Info "Running test suite..."
$testOutput = uv run pytest -q 2>&1
if ($LASTEXITCODE -ne 0) {
    $testOutput | ForEach-Object { Write-Host $_ }
    Fail "Test suite failed"
}
$testResult = $testOutput | Select-Object -Last 1
Ok $testResult

# Prevent accidental pushes only after all validation gates pass.
git -C $VAULT_ROOT remote set-url --push origin no-push
Ok "Push disabled (no-push) — installed vault cannot push to public remote"

# ─── 5. Set environment variables ──────────────────────────────────

Info "Setting environment variables..."

# Warn if env vars already point somewhere else (avoid silent clobber)
$oldRoot = [Environment]::GetEnvironmentVariable("LLM_WIKI_ROOT", "User")
if ($oldRoot -and $oldRoot -ne $VAULT_ROOT) {
    Warn "LLM_WIKI_ROOT was '$oldRoot', overwriting to '$VAULT_ROOT'"
}
$oldState = [Environment]::GetEnvironmentVariable("LLM_WIKI_STATE_ROOT", "User")
if ($oldState -and $oldState -ne $VAULT_ROOT) {
    Warn "LLM_WIKI_STATE_ROOT was '$oldState', overwriting to '$VAULT_ROOT'"
}

[Environment]::SetEnvironmentVariable("LLM_WIKI_ROOT", $VAULT_ROOT, "User")
# Runtime lives inside the vault as gitignored cache/logs/run dirs.
# LLM_WIKI_STATE_ROOT defaults to the vault itself; only set it explicitly
# if you want runtime on a different disk.
[Environment]::SetEnvironmentVariable("LLM_WIKI_STATE_ROOT", $VAULT_ROOT, "User")
[Environment]::SetEnvironmentVariable("MEMORY_LLM_PROVIDER", "opencode-sdk", "User")
$env:LLM_WIKI_ROOT = $VAULT_ROOT
$env:LLM_WIKI_STATE_ROOT = $VAULT_ROOT
$env:MEMORY_LLM_PROVIDER = "opencode-sdk"

$STATE_ROOT = $VAULT_ROOT
New-Item -ItemType Directory -Path "$STATE_ROOT\run" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\run\queue" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\logs" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\cache" -Force | Out-Null
New-Item -ItemType Directory -Path "$STATE_ROOT\cache\cognee" -Force | Out-Null
Ok "LLM_WIKI_ROOT set (User scope); runtime at $STATE_ROOT\{run,logs,cache} (gitignored)"

# ─── 6. Build search index ─────────────────────────────────────────

Info "Building search index..."
uv run python scripts\search_memory.py --rebuild 2>$null | Out-Null
Ok "Search index built"

# ─── 7. Register Task Scheduler ────────────────────────────────────

Info "Registering Windows Task Scheduler..."
$pythonExe = (Get-Command python).Source
try {
    & ".\scripts\install-scheduled-tasks.ps1" 2>$null
    if ($LASTEXITCODE -eq 0) { Ok "Task Scheduler: nightly 03:00 + weekly Sun 04:00" }
    else { Warn "Task Scheduler registration failed — run scripts\install-scheduled-tasks.ps1 manually" }
} catch {
    Warn "Task Scheduler registration failed — run scripts\install-scheduled-tasks.ps1 manually"
}

# ─── 8. Detect and wire up agents ──────────────────────────────────

Info "Detecting agents..."
$agents = @()

# OpenCode — install to effective XDG config and the Windows compatibility path.
$openCodeConfigs = @(Get-OpenCodeConfigs)
$openCodeConfig = $null
$openCodePluginSrc = Join-Path $VAULT_ROOT "scripts\llm-wiki-memory-opencode.js"
if ((Get-Process "OpenCode*" -ErrorAction SilentlyContinue) -or ($openCodeConfigs | Where-Object { Test-Path $_ }) -or (Get-Command opencode -ErrorAction SilentlyContinue)) {
    $agents += "OpenCode"
    if (Test-Path $openCodePluginSrc) {
        foreach ($openCodeConfig in $openCodeConfigs) {
            $pluginDir = Join-Path $openCodeConfig "plugins"
            New-Item -ItemType Directory -Path $pluginDir -Force | Out-Null
            $pluginDst = Join-Path $pluginDir "llm-wiki-memory.js"
            Copy-Item -LiteralPath $openCodePluginSrc -Destination $pluginDst -Force
            Ok "OpenCode plugin installed → $pluginDst"
        }
        # Generate initial context file so the first session has context
        $ctxFile = Join-Path $STATE_ROOT "cache\session-context.md"
        try { & uv run python (Join-Path $VAULT_ROOT "scripts\session_start_context.py") --output-file $ctxFile 2>$null | Out-Null } catch {}
    } else {
        Warn "OpenCode detected but plugin source missing: $openCodePluginSrc"
    }
}

# Codex
$codexConfig = Join-Path $env:USERPROFILE ".codex"
if ((Get-Command codex -ErrorAction SilentlyContinue) -or (Test-Path $codexConfig)) {
    $agents += "Codex"
    # Add wrapper to profile
    $profilePath = $PROFILE
    if (Update-PowerShellProfile -ProfilePath $profilePath) {
        Ok "Codex wrapper added to $profilePath"
    }
    Info "Merging native LLM-wiki hooks into Codex config (backup first)..."
    uv run python (Join-Path $VAULT_ROOT "scripts\merge_codex_hooks.py") `
        --vault-root $VAULT_ROOT 2>&1 | ForEach-Object { Info "$_" }
    if ($LASTEXITCODE -eq 0) {
        Ok "Codex hooks merged → $codexConfig\hooks.json"
        Warn "Review and trust the new hooks with /hooks in Codex."
    } else {
        Warn "Codex hooks merge failed — run uv run python scripts\merge_codex_hooks.py"
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
        --state-root $STATE_ROOT `
        --legacy-shell powershell 2>&1 | ForEach-Object { Info "$_" }
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

# Antigravity
if (Test-Path "$env:USERPROFILE\.antigravity" -ErrorAction SilentlyContinue) {
    $agents += "Antigravity"
    Ok "Antigravity detected — copy integrations\antigravity\AGENTS.md to your project root"
}

if ($agents.Count -eq 0) {
    Warn "No agents detected. Install OpenCode, Codex, Claude Code, Cursor, or Antigravity."
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
Write-Host "State:       $VAULT_ROOT (cache/logs/run, gitignored)"
Write-Host "Agents:      $($agents -join ', ')"
Write-Host "Maintenance: Task Scheduler (nightly + weekly)"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Restart terminal"
Write-Host "  2. Open a project in your agent"
Write-Host "  3. Work normally — capture is automatic"
Write-Host ""
