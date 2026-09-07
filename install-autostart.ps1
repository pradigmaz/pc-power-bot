[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PythonPath,

    [ValidatePattern('^[^\\/:*?"<>|]+$')]
    [string]$TaskName = 'PcPowerBot'
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

if (Get-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction SilentlyContinue) {
    throw "Task $taskPath$TaskName already exists. It was not replaced."
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $python -Argument ('"{0}"' -f $entryPoint) -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel LeastPrivilege
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Local Telegram PC power bot' | Out-Null
Write-Host "Created $taskPath$TaskName for $currentUser. It starts after this user signs in."
