# Install-KeepWSLAlive.ps1
# Register (or replace) the KeepWSLAlive scheduled task so the WSL VM stays
# resident at logon without flashing a wsl.exe console window. The task
# launches the sibling KeepWSLAlive.vbs via wscript, which calls wsl.exe
# with SW_HIDE.
#
# Run from Windows PowerShell. No admin needed — the task is user-scope.
#   PowerShell -ExecutionPolicy Bypass -File .\Install-KeepWSLAlive.ps1

$ErrorActionPreference = 'Stop'

$VbsPath = Join-Path $PSScriptRoot 'KeepWSLAlive.vbs'
if (-not (Test-Path $VbsPath)) {
    Write-Error "KeepWSLAlive.vbs not found next to this script ($VbsPath)."
    exit 1
}

$TaskName = 'KeepWSLAlive'
$User     = "$env:USERDOMAIN\$env:USERNAME"

$Action    = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$VbsPath`""
$Trigger   = New-ScheduledTaskTrigger -AtLogOn -User $User
$Settings  = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero)
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing $TaskName task."
}

Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $Action `
    -Trigger   $Trigger `
    -Settings  $Settings `
    -Principal $Principal | Out-Null

Write-Host "Installed $TaskName. Logon trigger now runs:"
Write-Host "  wscript.exe `"$VbsPath`""
Write-Host "Trigger it now without re-logging in:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
