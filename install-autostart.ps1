[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PythonPath,

    [ValidatePattern('^[^\\/:*?"<>|]+$')]
    [string]$TaskName = 'PcPowerBot',

    [switch]$ReplaceExisting
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$entryPoint = Join-Path $projectRoot 'bot.py'
$taskPath = '\'

if (-not (Test-Path -LiteralPath $entryPoint -PathType Leaf)) {
    throw "Missing bot entry point: $entryPoint"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'PythonPath must point to python.exe.'
}

$python = (Resolve-Path -LiteralPath $PythonPath).Path
& $python --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'PythonPath did not start successfully.'
}

$pythonWindowless = Join-Path -Path (Split-Path -Parent $python) -ChildPath 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) {
    throw "Missing pythonw.exe next to $python. It is required for windowless autostart."
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction SilentlyContinue
if ($existingTask -and -not $ReplaceExisting) {
    throw "Task $taskPath$TaskName already exists. It was not replaced."
}
if ($existingTask -and $existingTask.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName -TaskPath $taskPath
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ((Get-ScheduledTask -TaskName $TaskName -TaskPath $taskPath).State -ne 'Running') {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if ((Get-ScheduledTask -TaskName $TaskName -TaskPath $taskPath).State -eq 'Running') {
        throw "Task $taskPath$TaskName did not stop before update."
    }
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('"{0}"' -f $entryPoint) -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Local Telegram PC power bot' -Force:$ReplaceExisting | Out-Null
Start-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction Stop
if ($existingTask) {
    Write-Host "Updated $taskPath$TaskName for $currentUser and started it without a console window."
} else {
    Write-Host "Created $taskPath$TaskName for $currentUser and started it without a console window."
}
