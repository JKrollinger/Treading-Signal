# Registers a Windows Task Scheduler job that starts scheduler.py (hidden, no console)
# every time you log on, so the daily pre-market scan keeps running after reboots.
#
#   Run once from this folder:   powershell -ExecutionPolicy Bypass -File .\register_task.ps1
#   Remove the job:              Unregister-ScheduledTask -TaskName "PremarketScanner" -Confirm:$false
#   Logs:                        logs\scheduler.log

$root   = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
$script = Join-Path $root "scheduler.py"

if (-not (Test-Path $python)) {
    Write-Error "Virtualenv not found at $python. Create it first: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$action   = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "PremarketScanner" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "PremarketScanner"
Write-Host "Registered and started task 'PremarketScanner'. It runs scheduler.py at every logon."
