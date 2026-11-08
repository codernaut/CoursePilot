<#
.SYNOPSIS
  Registers CoursePilot (web app + worker) as two Windows Scheduled Tasks that start at boot and
  restart when they fail. No extra software needed.

.DESCRIPTION
  Run from an elevated (Administrator) PowerShell, after setup.bat has been run in the install
  folder (it creates backend\.venv). Output goes to <InstallDir>\logs\web.log and worker.log.

  .\register-windows-tasks.ps1 -InstallDir C:\CoursePilot
  .\register-windows-tasks.ps1 -InstallDir C:\CoursePilot -Unregister

  The tasks run as SYSTEM by default (convenient, but a web app should not need that much
  power: prefer a dedicated low-privilege account with write access to the install folder); pass -User 'DOMAIN\svc-coursepilot' -Password (Read-Host -AsSecureString) to
  use a service account instead. Note that stopping a task ends the process abruptly: a job that
  was running is picked up again automatically after the heartbeat timeout (about 2 minutes).
  For a graceful worker stop, prefer NSSM (see docs/deploy.md).
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$InstallDir,
  [string]$User = 'SYSTEM',
  [securestring]$Password,
  [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$names = @{ Web = 'CoursePilot Web'; Worker = 'CoursePilot Worker' }

if ($Unregister) {
  foreach ($name in $names.Values) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
      Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
      Unregister-ScheduledTask -TaskName $name -Confirm:$false
      Write-Host "Removed task '$name'."
    }
  }
  return
}

$InstallDir = (Resolve-Path $InstallDir).Path
$backend = Join-Path $InstallDir 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$logs = Join-Path $InstallDir 'logs'
if (-not (Test-Path $python)) { throw "Not found: $python. Run setup.bat in $InstallDir first." }
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function New-CoursePilotTask([string]$Name, [string]$Command, [string]$LogFile) {
  # cmd /c lets us append stdout and stderr to a log file.
  $arguments = "/c `"`"$python`" $Command >> `"$LogFile`" 2>&1`""
  $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arguments -WorkingDirectory $backend
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  $params = @{ TaskName = $Name; Action = $action; Trigger = $trigger; Settings = $settings; Force = $true }
  if ($User -eq 'SYSTEM') {
    $params.Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
  } else {
    $params.User = $User
    if ($Password) {
      $params.Password = [System.Net.NetworkCredential]::new('', $Password).Password
    }
    $params.RunLevel = 'Limited'  # a service account needs no elevation
  }
  Register-ScheduledTask @params | Out-Null
  Write-Host "Registered task '$Name'."
}

# The web task migrates the database first; the worker starts a little later.
New-CoursePilotTask $names.Web '-m app.cli serve' (Join-Path $logs 'web.log')
New-CoursePilotTask $names.Worker '-m app.worker' (Join-Path $logs 'worker.log')

Start-ScheduledTask -TaskName $names.Web
Start-Sleep -Seconds 15
Start-ScheduledTask -TaskName $names.Worker
Write-Host 'Started. Check http://127.0.0.1:8000/api/v1/health/ready'
