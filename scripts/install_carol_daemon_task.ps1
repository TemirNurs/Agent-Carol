# Install Carol daemon as a Windows Scheduled Task so it survives logoff / sleep / reboot.
# Run once as Administrator: powershell -ExecutionPolicy Bypass -File install_carol_daemon_task.ps1

$TaskName  = "CarolDaemon"
$Workdir   = "C:\Agent Carol"
$Python    = (Get-Command python).Source
$Script    = "$Workdir\carol_daemon.py"
$LogFile   = "$Workdir\data\logs\carol_daemon.log"

# Wrapper that starts the daemon and keeps stdout/stderr redirected
$WrapperCmd = "cmd.exe /c cd /d `"$Workdir`" && `"$Python`" `"$Script`" >> `"$LogFile`" 2>&1"

# Wrap with PID-cleanup + restart-on-crash loop so the daemon stays alive
# even if a stale carol.pid is leftover or the script crashes
$Action    = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c cd /d `"$Workdir`" && del /q `"$Workdir\data\carol.pid`" 2>nul & :loop & `"$Python`" `"$Script`" >> `"$LogFile`" 2>&1 & echo [%date% %time%] daemon exited, restarting in 30s >> `"$LogFile`" & timeout /t 30 /nobreak >nul & del /q `"$Workdir\data\carol.pid`" 2>nul & goto loop"
$Trigger1  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger1 -Settings $Settings -Principal $Principal -Force -Description "Carol estimating daemon (heartbeat, bid scraping, daily briefing)"

Write-Host ""
Write-Host "Installed Task: $TaskName"
Write-Host "  Runs at: logon + startup"
Write-Host "  Auto-restart: 5 times, 1-minute interval"
Write-Host "  Log: $LogFile"
Write-Host ""
Write-Host "Start now:  schtasks /Run /TN $TaskName"
Write-Host "Status:     schtasks /Query /TN $TaskName /V /FO LIST"
Write-Host "Stop:       schtasks /End /TN $TaskName"
Write-Host "Remove:     schtasks /Delete /TN $TaskName /F"
