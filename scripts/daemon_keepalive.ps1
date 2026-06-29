# daemon_keepalive.ps1 - runs every 5 min via Task Scheduler.
# If the carol_daemon process is dead, restart it.
# This guards against the daemon silently dying (which happened today at 08:44
# and went unnoticed until the user complained that CRM wasn't updating).

$Workdir   = "C:\Agent Carol"
$PidFile   = "$Workdir\data\carol.pid"
$LogFile   = "$Workdir\data\logs\carol_daemon.log"
$KeepaliveLog = "$Workdir\data\logs\daemon_keepalive.log"
$Python    = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = "python" }
$Script    = "$Workdir\carol_daemon.py"

function Write-KaLog($msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg
    Add-Content -Path $KeepaliveLog -Value $line -Encoding UTF8
}

# Check PID file
$alive = $false
if (Test-Path $PidFile) {
    $thePid = (Get-Content $PidFile -Raw).Trim()
    if ($thePid -match '^\d+$') {
        $proc = Get-Process -Id $thePid -ErrorAction SilentlyContinue
        if ($proc) { $alive = $true }
    }
}

if ($alive) {
    Write-KaLog "OK - daemon running (PID $thePid)"
    exit 0
}

# Daemon dead - restart it
Write-KaLog "DAEMON DEAD - restarting"
Remove-Item $PidFile -ErrorAction SilentlyContinue

# Start as detached background process
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $Python
$startInfo.Arguments = "`"$Script`""
$startInfo.WorkingDirectory = $Workdir
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$proc = [System.Diagnostics.Process]::Start($startInfo)

if ($proc) {
    Write-KaLog "STARTED - new PID $($proc.Id)"
    # Telegram ping so user knows daemon was auto-recovered
    $token = $env:TELEGRAM_BOT_TOKEN
    if (-not $token) { $token = "" }
    $chat = $env:USER_TELEGRAM_CHAT_ID
    if (-not $chat) { $chat = "" }
    try {
        $body = @{
            chat_id = $chat
            text = "Carol daemon auto-recovered. Daemon was dead; keepalive restarted it (PID $($proc.Id))."
            parse_mode = "Markdown"
        } | ConvertTo-Json
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" `
            -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10 | Out-Null
    } catch {}
} else {
    Write-KaLog "FAILED to start daemon"
}
