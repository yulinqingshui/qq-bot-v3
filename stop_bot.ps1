<# QQ Bot v3 stopper: stop bot gracefully (control API) + kill GUI/bot processes #>
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
cd $root

# read control-API port from config.yaml (control_api.port; fallback 8697)
$port = 8697
$cfg = Join-Path $root 'config.yaml'
if (Test-Path $cfg) {
    $text = Get-Content $cfg -Raw
    # (?m) makes ^ match line starts; (?s) lets .* cross newlines.
    # First indented "port:" after the "control_api:" header is ours.
    if ($text -match "(?ms)^control_api:.*?^\s+port:\s*(\d+)") { $port = [int]$Matches[1] }
}

# 1) graceful bot shutdown via control API
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/restart" -Method Post -TimeoutSec 8
    if ($r.ok) { Write-Host "bot stop requested via control API (port $port)" }
} catch { Write-Host "control API not reachable: $($_.Exception.Message)" }

# 2) kill remaining GUI / bot processes (by python.exe command line match)
Start-Sleep -Seconds 3
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like "*qq-bot-v3*" -and ($_.CommandLine -like "*gui_launcher.py*" -or $_.CommandLine -like "*core/bot.py*" -or $_.CommandLine -like "*core\bot.py*")
} | ForEach-Object {
    Write-Host "stopping pid $($_.ProcessId): $($_.CommandLine.Substring(0, [Math]::Min(80, $_.CommandLine.Length)))"
    Stop-Process -Id $_.ProcessId -Force
}

# 3) kill leftover NapCat node.exe (green version is a child of bot;
#    if bot was hard-killed first, node becomes orphan -- 08-24 fix)
Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object {
    $_.CommandLine -like "*napcat_win*"
} | ForEach-Object {
    Write-Host "stopping NapCat pid $($_.ProcessId): $($_.CommandLine.Substring(0, [Math]::Min(80, $_.CommandLine.Length)))"
    Stop-Process -Id $_.ProcessId -Force
}
Write-Host "done."
