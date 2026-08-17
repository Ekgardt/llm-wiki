# Installs Windows Task Scheduler entries for fully-automatic memory maintenance.
#
# Creates two tasks:
#   - LLMWiki-Nightly: runs nightly at 03:00 - queue drain + compile + lint
#   - LLMWiki-Weekly:  runs every Sunday 04:00 - deep maintenance + OKF sweep
#
# Both run as the current user (no admin elevation needed) and only when
# the user is logged on. Output goes to $env:LLM_WIKI_STATE_ROOT\logs\.
#
# Usage:
#   . $env:LLM_WIKI_ROOT\scripts\install-scheduled-tasks.ps1                # install
#   . $env:LLM_WIKI_ROOT\scripts\install-scheduled-tasks.ps1 -Uninstall     # remove
#   . $env:LLM_WIKI_ROOT\scripts\install-scheduled-tasks.ps1 -Status         # check state
#
# Requires: Windows Task Scheduler service running (default on).

param(
    [Parameter(Mandatory = $true)][string]$VaultRoot,
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [Parameter(Mandatory = $true)][string]$UvPath,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$StateJson,
    [switch]$RunNightlyNow,
    [switch]$RunWeeklyNow
)

$ErrorActionPreference = "Stop"
$tasks = @("LLMWiki-Nightly", "LLMWiki-Weekly")

# Detect dot-sourcing at TOP LEVEL (outside any function).
# Inside a function, $MyInvocation.CommandOrigin is always 'Internal',
# so we must capture the flag here, before defining _SafeExit.
# When dot-sourced: CommandOrigin = 'Internal' (runs in caller's scope).
# When run as child process: CommandOrigin = 'Runspace'.
$script:IsDotSourced = $MyInvocation.CommandOrigin -eq 'Internal'

function New-LLMWikiScheduledAction {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("nightly", "weekly")]
        [string]$Kind,
        [Parameter(Mandatory = $true)][string]$VaultRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$UvPath,
        [Parameter(Mandatory = $true)][string]$RunnerPath,
        [Parameter(Mandatory = $true)][string]$PowerShellPath
    )
    $runnerLiteral = $RunnerPath.Replace("'", "''")
    $kindLiteral = $Kind.Replace("'", "''")
    $rootLiteral = $VaultRoot.Replace("'", "''")
    $stateLiteral = $StateRoot.Replace("'", "''")
    $uvLiteral = $UvPath.Replace("'", "''")
    $command = "& '$runnerLiteral' -Kind '$kindLiteral' -VaultRoot '$rootLiteral' " +
        "-StateRoot '$stateLiteral' -UvPath '$uvLiteral'"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    return New-ScheduledTaskAction `
        -Execute $PowerShellPath `
        -Argument "-NoProfile -NonInteractive -EncodedCommand $encoded"
}

function Test-LLMWikiScheduledTasks {
    param(
        [Parameter(Mandatory = $true)][string]$VaultRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$UvPath
    )
    $verified = $true
    $specifications = @(
        @{ Name = "LLMWiki-Nightly"; Kind = "nightly" },
        @{ Name = "LLMWiki-Weekly"; Kind = "weekly" }
    )
    foreach ($specification in $specifications) {
        $name = $specification.Name
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Host "  ${name}: NOT INSTALLED" -ForegroundColor Yellow
            $verified = $false
            continue
        }
        try {
            $info = $task | Get-ScheduledTaskInfo -ErrorAction Stop
        } catch {
            Write-Host "  ${name}: STATUS UNAVAILABLE" -ForegroundColor Yellow
            $verified = $false
            continue
        }
        $taskValid = $true
        if ([string]::IsNullOrWhiteSpace([string]$task.State) -or $task.State -eq "Disabled") {
            $taskValid = $false
        }
        if ($task.Actions.Count -ne 1 -or $task.Triggers.Count -ne 1) {
            $taskValid = $false
        } else {
            $action = $task.Actions[0]
            $trigger = $task.Triggers[0]
            if ([string]::IsNullOrWhiteSpace([string]$action.Execute) -or
                [string]$action.Arguments -notmatch '-EncodedCommand\s+(\S+)\s*$') {
                $taskValid = $false
            } else {
                try {
                    $decoded = [Text.Encoding]::Unicode.GetString(
                        [Convert]::FromBase64String($Matches[1])
                    )
                    foreach ($expected in @(
                        $specification.Kind,
                        [System.IO.Path]::GetFullPath($VaultRoot),
                        [System.IO.Path]::GetFullPath($StateRoot),
                        [System.IO.Path]::GetFullPath($UvPath)
                    )) {
                        if (-not $decoded.Contains($expected)) { $taskValid = $false }
                    }
                } catch {
                    $taskValid = $false
                }
            }
            if ([string]::IsNullOrWhiteSpace([string]$trigger.StartBoundary) -or
                $trigger.Enabled -eq $false) {
                $taskValid = $false
            }
        }
        if ([string]$task.Principal.LogonType -ne "Interactive" -or
            [string]::IsNullOrWhiteSpace([string]$task.Principal.UserId)) {
            $taskValid = $false
        }
        Write-Host "  ${name}:" -ForegroundColor $(if ($taskValid) { "Green" } else { "Yellow" })
        Write-Host "    State:        $($task.State)"
        Write-Host "    Logon type:   $($task.Principal.LogonType)"
        Write-Host "    Last run:     $($info.LastRunTime)"
        Write-Host "    Last result:  $($info.LastTaskResult)"
        Write-Host "    Next run:     $($info.NextRunTime)"
        if (-not $taskValid) { $verified = $false }
    }
    return $verified
}

function Get-LLMWikiScheduledTaskState {
    param(
        [Parameter(Mandatory = $true)][string]$VaultRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$UvPath
    )
    $existing = @(
        $tasks | ForEach-Object {
            Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
        } | Where-Object { $null -ne $_ }
    )
    if ($existing.Count -eq 0) { return "absent" }
    if ($existing.Count -ne $tasks.Count) { return "conflict" }
    $verified = Test-LLMWikiScheduledTasks `
        -VaultRoot $VaultRoot `
        -StateRoot $StateRoot `
        -UvPath $UvPath 6>$null
    if ($verified) { return "equivalent" }
    return "conflict"
}

if ($StateJson) {
    $state = Get-LLMWikiScheduledTaskState `
        -VaultRoot $VaultRoot `
        -StateRoot $StateRoot `
        -UvPath $UvPath
    [Console]::Out.WriteLine((@{ state = $state } | ConvertTo-Json -Compress))
    if ($script:IsDotSourced) { return } else { exit 0 }
}

if ($Status) {
    Write-Host "=== Scheduled task status ===" -ForegroundColor Cyan
    $verified = Test-LLMWikiScheduledTasks `
        -VaultRoot $VaultRoot `
        -StateRoot $StateRoot `
        -UvPath $UvPath
    if ($script:IsDotSourced) { return $verified }
    if ($verified) { exit 0 }
    exit 1
}

if ($Uninstall) {
    $currentState = Get-LLMWikiScheduledTaskState `
        -VaultRoot $VaultRoot `
        -StateRoot $StateRoot `
        -UvPath $UvPath
    if ($currentState -eq "conflict") {
        throw "Scheduled task ownership is ambiguous; refusing uninstall"
    }
    if ($currentState -eq "absent") {
        if ($script:IsDotSourced) { return } else { exit 0 }
    }
    Write-Host "Uninstalling scheduled tasks..." -ForegroundColor Cyan
    foreach ($name in $tasks) {
        try {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
            Write-Host "  removed: $name" -ForegroundColor Green
        } catch {
            Write-Host "  (not installed: $name)" -ForegroundColor DarkGray
        }
    }
    if ($script:IsDotSourced) { return } else { exit 0 }
}

# Resolve paths.
$VaultRoot = [System.IO.Path]::GetFullPath($VaultRoot)
$StateRoot = [System.IO.Path]::GetFullPath($StateRoot)
$UvPath = [System.IO.Path]::GetFullPath($UvPath)
$runnerPath = Join-Path $VaultRoot "scripts\run-scheduled-task.ps1"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) { throw "Missing: $runnerPath" }
if (-not (Test-Path -LiteralPath $UvPath -PathType Leaf)) { throw "Missing: $UvPath" }
$powerShellPath = (Get-Process -Id $PID).Path
$currentState = Get-LLMWikiScheduledTaskState `
    -VaultRoot $VaultRoot `
    -StateRoot $StateRoot `
    -UvPath $UvPath
if ($currentState -eq "conflict") {
    throw "Scheduled task ownership is ambiguous; refusing registration"
}
if ($currentState -eq "equivalent") {
    Write-Host "Scheduled tasks already match the LLM-Wiki contract." -ForegroundColor Green
    if ($script:IsDotSourced) { return } else { exit 0 }
}

# --- Nightly task: 03:00 every day ---
$nightlyAction = New-LLMWikiScheduledAction `
    -Kind nightly `
    -VaultRoot $VaultRoot `
    -StateRoot $StateRoot `
    -UvPath $UvPath `
    -RunnerPath $runnerPath `
    -PowerShellPath $powerShellPath

$nightlyTrigger = New-ScheduledTaskTrigger -Daily -At 3am

$nightlySettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15)

$nightlyPrincipal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Write-Host "Registering LLMWiki-Nightly (daily 03:00)..." -ForegroundColor Cyan
Register-ScheduledTask `
    -TaskName "LLMWiki-Nightly" `
    -Action $nightlyAction `
    -Trigger $nightlyTrigger `
    -Settings $nightlySettings `
    -Principal $nightlyPrincipal `
    -Description "LLM-wiki: nightly queue drain + compile + lint. No user interaction required." |
    Out-Null
Write-Host "  registered" -ForegroundColor Green

# --- Weekly task: Sunday 04:00 ---
$weeklyAction = New-LLMWikiScheduledAction `
    -Kind weekly `
    -VaultRoot $VaultRoot `
    -StateRoot $StateRoot `
    -UvPath $UvPath `
    -RunnerPath $runnerPath `
    -PowerShellPath $powerShellPath

$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 4am

$weeklySettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 30)

Write-Host "Registering LLMWiki-Weekly (Sunday 04:00)..." -ForegroundColor Cyan
Register-ScheduledTask `
    -TaskName "LLMWiki-Weekly" `
    -Action $weeklyAction `
    -Trigger $weeklyTrigger `
    -Settings $weeklySettings `
    -Principal $nightlyPrincipal `
    -Description "LLM-wiki: weekly deep maintenance + OKF conformance sweep + lint." |
    Out-Null
Write-Host "  registered" -ForegroundColor Green

# --- Optional: run now to verify ---
if ($RunNightlyNow) {
    Write-Host "Starting LLMWiki-Nightly now..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName "LLMWiki-Nightly"
}
if ($RunWeeklyNow) {
    Write-Host "Starting LLMWiki-Weekly now..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName "LLMWiki-Weekly"
}

Write-Host ""
Write-Host "Done. Tasks registered for the current logged-on user:" -ForegroundColor Green
Write-Host "  LLMWiki-Nightly: every day at 03:00"
Write-Host "  LLMWiki-Weekly:  every Sunday at 04:00"
Write-Host ""
Write-Host "Check status:  .\install-scheduled-tasks.ps1 -Status"
Write-Host "Uninstall:     .\install-scheduled-tasks.ps1 -Uninstall"
Write-Host ""
Write-Host "Reports land at: $env:LLM_WIKI_STATE_ROOT\logs\nightly-*.md"
