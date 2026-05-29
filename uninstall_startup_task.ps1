# uninstall_startup_task.ps1 — Remove both auto-start methods.
$taskName     = "MakiAutoStart"
$shortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "Maki.lnk"

# 1. Startup-folder shortcut
if (Test-Path $shortcutPath) {
    Remove-Item $shortcutPath -Force
    Write-Host "Removed shortcut: $shortcutPath" -ForegroundColor Green
} else {
    Write-Host "No Startup-folder shortcut found." -ForegroundColor Yellow
}

# 2. Task Scheduler task
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed Task Scheduler task: $taskName" -ForegroundColor Green
} else {
    Write-Host "No Task Scheduler task found." -ForegroundColor Yellow
}

Write-Host "Maki auto-start has been fully uninstalled."
