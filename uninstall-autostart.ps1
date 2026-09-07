[CmdletBinding()]
param(
    [ValidatePattern('^[^\\/:*?"<>|]+$')]
    [string]$TaskName = 'PcPowerBot'
)

$ErrorActionPreference = 'Stop'
$taskPath = '\'
$task = Get-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction SilentlyContinue

if ($null -eq $task) {
    Write-Host "Task $taskPath$TaskName does not exist. Nothing was removed."
    return
}

if ($task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName -TaskPath $taskPath
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ((Get-ScheduledTask -TaskName $TaskName -TaskPath $taskPath).State -ne 'Running') {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if ((Get-ScheduledTask -TaskName $TaskName -TaskPath $taskPath).State -eq 'Running') {
        throw "Task $taskPath$TaskName did not stop before removal."
    }
}

Unregister-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -Confirm:$false
Write-Host "Removed $taskPath$TaskName. Project files, .env, and state.json were not changed."
