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
    # Which contract the tasks are judged by. 2 is the current one: a marker in the
    # description and the time limits below. 1 is what machines installed before
    # 2026-09-17 carry; the install control plane passes it to take such tasks back.
    [ValidateSet(1, 2)][int]$SpecVersion = 2,
    [ValidateScript({ $_ -gt 0 })][int]$NightlyLimitHours,
    [ValidateScript({ $_ -gt 0 })][int]$WeeklyLimitHours,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$StateJson,
    [switch]$RunNightlyNow,
    [switch]$RunWeeklyNow
)

$ErrorActionPreference = "Stop"
$tasks = @("LLMWiki-Nightly", "LLMWiki-Weekly")

# How long each task may run: install_control.SCHEDULER_LIMIT_HOURS, the one table
# every scheduler uses, and the only place these hours are written in this script.
# Each sits above its pass's own bound (scheduled_nightly.worst_case_seconds,
# scheduled_weekly.worst_case_seconds); tests hold both facts.
# A function, not only a script variable: the status check calls it, so a caller
# that loads the check alone still reads the same table.
function Get-LLMWikiLimitHours { return @{ nightly = 5; weekly = 6 } }
function Get-LLMWikiConfiguredLimitHours {
    $limits = Get-LLMWikiLimitHours
    if ($NightlyLimitHours -gt 0) { $limits.nightly = $NightlyLimitHours }
    if ($WeeklyLimitHours -gt 0) { $limits.weekly = $WeeklyLimitHours }
    return $limits
}
$LimitHours = Get-LLMWikiConfiguredLimitHours

# Captured at top level, where $MyInvocation describes this script rather than a function.
# CommandOrigin cannot tell dot-sourcing apart: a script with mandatory parameters run by
# `pwsh -File` reports 'Internal' too, so -Status exited 0 for missing tasks.
$script:IsDotSourced = $MyInvocation.InvocationName -eq '.'

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

function Test-LLMWikiTaskSpec {
    # The time limit was not part of what "equivalent" meant, so a machine registered
    # with one-hour tasks passed as up to date and kept killing the nightly pass.
    # Version 2 tasks carry a marker and the contract's limit; version 1 tasks are
    # exactly those without the marker, so the two never both claim the same tasks.
    # See docs/research/2026-09-17-a-changed-task-setting-reaches-an-installed-machine.md.
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][int]$SpecVersion,
        [Parameter(Mandatory = $true)][int]$LimitHours
    )
    $marked = ([string]$Task.Description).Contains("[llm-wiki-task-spec:2]")
    if ($SpecVersion -eq 1) { return -not $marked }
    if (-not $marked) { return $false }
    try {
        $limit = [System.Xml.XmlConvert]::ToTimeSpan([string]$Task.Settings.ExecutionTimeLimit)
    } catch [System.FormatException] {
        return $false
    }
    return $limit -eq (New-TimeSpan -Hours $LimitHours)
}

function Test-LLMWikiTaskAction {
    param($Action, [string[]]$Expected)
    if ([string]::IsNullOrWhiteSpace([string]$Action.Execute) -or
        [string]$Action.Arguments -notmatch '-EncodedCommand\s+(\S+)\s*$') { return $false }
    try {
        $decoded = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1]))
        return @($Expected | Where-Object { -not $decoded.Contains($_) }).Count -eq 0
    } catch { return $false }
}

function Test-LLMWikiTaskSchedule {
    param($Task, [string[]]$Expected)
    if ($Task.Actions.Count -ne 1 -or $Task.Triggers.Count -ne 1) { return $false }
    $trigger = $Task.Triggers[0]
    if ([string]::IsNullOrWhiteSpace([string]$trigger.StartBoundary) -or
        $trigger.Enabled -eq $false) { return $false }
    return Test-LLMWikiTaskAction -Action $Task.Actions[0] -Expected $Expected
}

function Test-LLMWikiTaskIdentity {
    param($Task)
    $stateValid = -not [string]::IsNullOrWhiteSpace([string]$Task.State) -and $Task.State -ne "Disabled"
    $principalValid = [string]$Task.Principal.LogonType -eq "Interactive" -and
        -not [string]::IsNullOrWhiteSpace([string]$Task.Principal.UserId)
    return $stateValid -and $principalValid
}

function Write-LLMWikiTaskStatus {
    param($Task, $Info, [string]$Name, [bool]$Valid)
    $color = if ($Valid) { "Green" } else { "Yellow" }
    Write-Host "  ${Name}:" -ForegroundColor $color
    Write-Host "    State:        $($Task.State)"
    Write-Host "    Logon type:   $($Task.Principal.LogonType)"
    Write-Host "    Last run:     $($Info.LastRunTime)"
    Write-Host "    Last result:  $($Info.LastTaskResult)"
    Write-Host "    Next run:     $($Info.NextRunTime)"
}

function Test-LLMWikiTaskRegistration {
    param($Specification, [string[]]$Paths, [int]$SpecVersion)
    $name = $Specification.Name
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "  ${name}: NOT INSTALLED" -ForegroundColor Yellow
        return $false
    }
    try { $info = $task | Get-ScheduledTaskInfo -ErrorAction Stop }
    catch {
        Write-Host "  ${name}: STATUS UNAVAILABLE" -ForegroundColor Yellow
        return $false
    }
    $checks = @(
        (Test-LLMWikiTaskIdentity -Task $task),
        (Test-LLMWikiTaskSchedule -Task $task -Expected (@($Specification.Kind) + $Paths)),
        (Test-LLMWikiTaskSpec -Task $task -SpecVersion $SpecVersion -LimitHours $Specification.LimitHours)
    )
    $valid = $checks -notcontains $false
    Write-LLMWikiTaskStatus -Task $task -Info $info -Name $name -Valid $valid
    return $valid
}

function Test-LLMWikiScheduledTasks {
    param(
        [Parameter(Mandatory = $true)][string]$VaultRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$UvPath,
        [ValidateSet(1, 2)][int]$SpecVersion = 2
    )
    $limits = Get-LLMWikiConfiguredLimitHours
    $specifications = @(
        @{ Name = "LLMWiki-Nightly"; Kind = "nightly"; LimitHours = $limits.nightly },
        @{ Name = "LLMWiki-Weekly"; Kind = "weekly"; LimitHours = $limits.weekly }
    )
    $paths = @($VaultRoot, $StateRoot, $UvPath) | ForEach-Object { [IO.Path]::GetFullPath($_) }
    $checks = @($specifications | ForEach-Object {
        Test-LLMWikiTaskRegistration -Specification $_ -Paths $paths -SpecVersion $SpecVersion
    })
    return $checks -notcontains $false
}

function Get-LLMWikiScheduledTaskState {
    param(
        [Parameter(Mandatory = $true)][string]$VaultRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$UvPath,
        [ValidateSet(1, 2)][int]$SpecVersion = 2
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
        -UvPath $UvPath `
        -SpecVersion $SpecVersion 6>$null
    return Get-LLMWikiVerificationState -Verified $verified
}

function Get-LLMWikiVerificationState([bool]$Verified) {
    if ($Verified) { return "equivalent" }
    return "conflict"
}

function New-LLMWikiOutcome([object[]]$Value, [int]$ExitCode) {
    return [pscustomobject]@{ Value = $Value; ExitCode = $ExitCode }
}

function Get-LLMWikiInstalledState {
    return Get-LLMWikiScheduledTaskState `
        -VaultRoot $VaultRoot `
        -StateRoot $StateRoot `
        -UvPath $UvPath `
        -SpecVersion $SpecVersion
}

function Write-LLMWikiTaskStateJson {
    $state = Get-LLMWikiInstalledState
    [Console]::Out.WriteLine((@{ state = $state } | ConvertTo-Json -Compress))
    return New-LLMWikiOutcome -Value @() -ExitCode 0
}

function Get-LLMWikiStatusExitCode([bool]$Verified) {
    if ($verified) { return 0 }
    return 1
}

function Show-LLMWikiTaskStatus {
    Write-Host "=== Scheduled task status ===" -ForegroundColor Cyan
    $verified = Test-LLMWikiScheduledTasks `
        -VaultRoot $VaultRoot `
        -StateRoot $StateRoot `
        -UvPath $UvPath `
        -SpecVersion $SpecVersion
    return New-LLMWikiOutcome -Value @($verified) -ExitCode (Get-LLMWikiStatusExitCode -Verified $verified)
}

function Remove-LLMWikiScheduledTask([string]$Name) {
    try {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop
        Write-Host "  removed: $Name" -ForegroundColor Green
    } catch {
        Write-Host "  (not installed: $Name)" -ForegroundColor DarkGray
    }
}

function Uninstall-LLMWikiScheduledTasks {
    $currentState = Get-LLMWikiInstalledState
    if ($currentState -eq "conflict") {
        throw "Scheduled task ownership is ambiguous; refusing uninstall"
    }
    if ($currentState -eq "absent") { return New-LLMWikiOutcome -Value @() -ExitCode 0 }
    Write-Host "Uninstalling scheduled tasks..." -ForegroundColor Cyan
    foreach ($name in $tasks) { Remove-LLMWikiScheduledTask -Name $name }
    return New-LLMWikiOutcome -Value @() -ExitCode 0
}

function Resolve-LLMWikiInstallPaths {
    $paths = @{
        VaultRoot = [System.IO.Path]::GetFullPath($VaultRoot)
        StateRoot = [System.IO.Path]::GetFullPath($StateRoot)
        UvPath = [System.IO.Path]::GetFullPath($UvPath)
    }
    $paths.RunnerPath = Join-Path $paths.VaultRoot "scripts\run-scheduled-task.ps1"
    if (-not (Test-Path -LiteralPath $paths.RunnerPath -PathType Leaf)) { throw "Missing: $($paths.RunnerPath)" }
    if (-not (Test-Path -LiteralPath $paths.UvPath -PathType Leaf)) { throw "Missing: $($paths.UvPath)" }
    $paths.PowerShellPath = (Get-Process -Id $PID).Path
    return $paths
}

function New-LLMWikiPathAction([string]$Kind, $Paths) {
    return New-LLMWikiScheduledAction `
        -Kind $Kind `
        -VaultRoot $Paths.VaultRoot `
        -StateRoot $Paths.StateRoot `
        -UvPath $Paths.UvPath `
        -RunnerPath $Paths.RunnerPath `
        -PowerShellPath $Paths.PowerShellPath
}

function Get-LLMWikiSpecMarker {
    # What Test-LLMWikiTaskSpec looks for. A version 1 registration happens only when the
    # control plane rolls an update back to a record that predates the marker.
    if ($SpecVersion -eq 2) { return " [llm-wiki-task-spec:2]" }
    return ""
}

function Register-LLMWikiTasks($Paths) {
    $specMarker = Get-LLMWikiSpecMarker

    # --- Nightly task: 03:00 every day ---
    $nightlyAction = New-LLMWikiPathAction -Kind nightly -Paths $Paths

    $nightlyTrigger = New-ScheduledTaskTrigger -Daily -At 3am

    # Above the pass's own bound in auto provider mode (scheduled_nightly.worst_case_seconds,
    # which counts the checkout update and the whole provider order one call may walk);
    # a one-hour limit killed the pass before it could release its lease or record a
    # result. The pass also stops itself at that bound, so this limit is only the backstop.
    # See docs/research/2026-09-14-the-scheduler-outlasts-the-pass.md and
    # docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md.
    $nightlySettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours $LimitHours.nightly) `
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
        -Description "LLM-wiki: nightly queue drain + compile + lint. No user interaction required.$specMarker" |
        Out-Null
    Write-Host "  registered" -ForegroundColor Green

    # --- Weekly task: Sunday 04:00 ---
    $weeklyAction = New-LLMWikiPathAction -Kind weekly -Paths $Paths

    $weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 4am

    # The weekly pass can wait for a whole nightly one and then run its own steps
    # (scheduled_weekly.worst_case_seconds). See
    # docs/research/2026-09-14-the-weekly-task-outlasts-its-pass.md and
    # docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md.
    $weeklySettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours $LimitHours.weekly) `
        -RestartCount 2 `
        -RestartInterval (New-TimeSpan -Minutes 30)

    Write-Host "Registering LLMWiki-Weekly (Sunday 04:00)..." -ForegroundColor Cyan
    Register-ScheduledTask `
        -TaskName "LLMWiki-Weekly" `
        -Action $weeklyAction `
        -Trigger $weeklyTrigger `
        -Settings $weeklySettings `
        -Principal $nightlyPrincipal `
        -Description "LLM-wiki: weekly deep maintenance + OKF conformance sweep + lint.$specMarker" |
        Out-Null
    Write-Host "  registered" -ForegroundColor Green
}

# --- Optional: run now to verify ---
function Start-LLMWikiRequestedTasks {
    if ($RunNightlyNow) {
        Write-Host "Starting LLMWiki-Nightly now..." -ForegroundColor Cyan
        Start-ScheduledTask -TaskName "LLMWiki-Nightly"
    }
    if ($RunWeeklyNow) {
        Write-Host "Starting LLMWiki-Weekly now..." -ForegroundColor Cyan
        Start-ScheduledTask -TaskName "LLMWiki-Weekly"
    }
}

function Write-LLMWikiInstallSummary {
    Write-Host ""
    Write-Host "Done. Tasks registered for the current logged-on user:" -ForegroundColor Green
    Write-Host "  LLMWiki-Nightly: every day at 03:00"
    Write-Host "  LLMWiki-Weekly:  every Sunday at 04:00"
    Write-Host ""
    Write-Host "Check status:  .\install-scheduled-tasks.ps1 -Status"
    Write-Host "Uninstall:     .\install-scheduled-tasks.ps1 -Uninstall"
    Write-Host ""
    Write-Host "Reports land at: $env:LLM_WIKI_STATE_ROOT\logs\nightly-*.md"
}

function Install-LLMWikiScheduledTasks {
    $paths = Resolve-LLMWikiInstallPaths
    $currentState = Get-LLMWikiInstalledState
    if ($currentState -eq "conflict") {
        throw "Scheduled task ownership is ambiguous; refusing registration"
    }
    if ($currentState -eq "equivalent") {
        Write-Host "Scheduled tasks already match the LLM-Wiki contract." -ForegroundColor Green
        return New-LLMWikiOutcome -Value @() -ExitCode 0
    }
    Register-LLMWikiTasks -Paths $paths
    Start-LLMWikiRequestedTasks
    Write-LLMWikiInstallSummary
    return New-LLMWikiOutcome -Value @() -ExitCode 0
}

function Invoke-LLMWikiRequestedMode {
    $modes = [ordered]@{
        "Write-LLMWikiTaskStateJson" = [bool]$StateJson
        "Show-LLMWikiTaskStatus" = [bool]$Status
        "Uninstall-LLMWikiScheduledTasks" = [bool]$Uninstall
    }
    $requested = @($modes.Keys | Where-Object { $modes[$_] }) + "Install-LLMWikiScheduledTasks"
    return & $requested[0]
}

$outcome = Invoke-LLMWikiRequestedMode
if ($script:IsDotSourced) { return $outcome.Value }
exit $outcome.ExitCode
