# QQ Bot v3 - create desktop shortcut (idempotent: skips if it exists)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$lnk  = $env:USERPROFILE + '\Desktop\QQ Bot.lnk'

if (Test-Path $lnk) {
    Write-Host "Desktop shortcut already exists: $lnk"
    exit 0
}

$ps = New-Object -ComObject WScript.Shell
$sc = $ps.CreateShortcut($lnk)
$sc.Description      = 'QQ Bot v3 console (windowless launcher)'
$sc.TargetPath       = $root + '\start_gui.vbs'
$sc.WorkingDirectory = $root
$sc.Save()
Write-Host "Desktop shortcut created: $lnk"
