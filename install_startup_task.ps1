# install_startup_task.ps1 — V7 auto-start install (belt-and-suspenders).
#
# Strategy: install BOTH methods so at least one fires after every reboot:
#   1. Shortcut in shell:startup  → fires reliably on every login
#   2. Task Scheduler task        → adds unlock + retry behavior
#
# Run from the project folder; no admin required.

$ErrorActionPreference = "Stop"

$taskName     = "MakiAutoStart"
$projectDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$vbsScript    = Join-Path $projectDir "start_maki_hidden.vbs"
$wscript      = Join-Path $env:SystemRoot "System32\wscript.exe"
$startupDir   = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "Maki.lnk"

# ── Sanity ─────────────────────────────────────────────────────────────────────
if (-not (Test-Path $vbsScript)) {
    Write-Error "Missing: $vbsScript"
    exit 1
}
if (-not (Test-Path $wscript)) {
    Write-Error "wscript.exe not found"
    exit 1
}

Write-Host "Project   : $projectDir"
Write-Host "Launcher  : $wscript ""$vbsScript"""
Write-Host "Startup   : $startupDir"
Write-Host ""

# ══════════════════════════════════════════════════════════════════════════════
# Method 1: Startup-folder shortcut (most reliable)
# ══════════════════════════════════════════════════════════════════════════════
Write-Host "[1/2] Installing Startup-folder shortcut..." -ForegroundColor Cyan

$wshShell           = New-Object -ComObject WScript.Shell
$sc                 = $wshShell.CreateShortcut($shortcutPath)
$sc.TargetPath      = $wscript
$sc.Arguments       = "`"$vbsScript`""
$sc.WorkingDirectory = $projectDir
$sc.WindowStyle     = 7   # minimized (won't actually show; VBS is hidden)
$sc.Description     = "Maki personal AI assistant"
$sc.Save()
Write-Host "    Created: $shortcutPath" -ForegroundColor Green

# ══════════════════════════════════════════════════════════════════════════════
# Method 2: Task Scheduler (adds unlock trigger)
# ══════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "[2/2] Installing Task Scheduler task (login + unlock)..." -ForegroundColor Cyan

# Remove prior version if any
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute  $wscript `
    -Argument "`"$vbsScript`"" `
    -WorkingDirectory $projectDir

# Login trigger with small delay
$loginTrigger       = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$loginTrigger.Delay = "PT8S"

# Unlock trigger via Security event 4801
$unlockTrigger = New-CimInstance -CimClass (
    Get-CimClass -ClassName MSFT_TaskEventTrigger `
        -Namespace Root/Microsoft/Windows/TaskScheduler
) -Property @{
    Enabled      = $true
    Subscription = @'
<QueryList>
  <Query Id="0" Path="Security">
    <Select Path="Security">*[System[EventID=4801]]</Select>
  </Query>
</QueryList>
'@
} -ClientOnly

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType Interactive `
    -RunLevel  Limited

try {
    Register-ScheduledTask `
        -TaskName    $taskName `
        -Action      $action `
        -Trigger     @($loginTrigger, $unlockTrigger) `
        -Settings    $settings `
        -Principal   $principal `
        -Description "Auto-start Maki at login and on unlock." `
        -Force | Out-Null
    Write-Host "    Task registered: $taskName" -ForegroundColor Green
} catch {
    Write-Warning "Task Scheduler registration failed: $($_.Exception.Message)"
    Write-Warning "Startup-folder shortcut will still fire at login (sufficient on its own)."
}

# ══════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " Maki auto-start installed." -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Test it right now without logging out:" -ForegroundColor Cyan
Write-Host "    & '$wscript' '$vbsScript'"
Write-Host ""
Write-Host "Verify the Task Scheduler task:" -ForegroundColor Cyan
Write-Host "    Get-ScheduledTask -TaskName $taskName"
Write-Host "    Start-ScheduledTask -TaskName $taskName"
Write-Host ""
Write-Host "Verify the Startup-folder shortcut:" -ForegroundColor Cyan
Write-Host "    Get-Item '$shortcutPath'"
Write-Host ""
Write-Host "Troubleshooting:" -ForegroundColor Yellow
Write-Host "  - If nothing launches: open the Startup folder by running 'shell:startup'"
Write-Host "    in Win+R and confirm Maki.lnk is there."
Write-Host "  - If pythonw.exe is not on PATH, edit start_maki_hidden.vbs"
Write-Host "    and hard-code the full python path."
Write-Host ""
Write-Host "Remove later:" -ForegroundColor Yellow
Write-Host "    powershell -ExecutionPolicy Bypass -File `"$projectDir\uninstall_startup_task.ps1`""
