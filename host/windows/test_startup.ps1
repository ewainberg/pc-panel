$ErrorActionPreference='Stop'
$controlFile=Join-Path $PSScriptRoot 'control.ps1'
$senderFile=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\rmine_telemetry_windows.py'))
& $controlFile -Stop
Start-Sleep -Seconds 3
$remaining=@(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object {$_.CommandLine -and $_.CommandLine.Contains($senderFile)})
if ($remaining.Count -ne 0) { throw 'Shutdown left sender processes running' }
Write-Output 'PASS: stop removed the complete sender process tree.'
$shortcut=Join-Path ([Environment]::GetFolderPath('Startup')) 'PC Panel Telemetry.lnk'
Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'PCPanel\stop.flag') -ErrorAction SilentlyContinue
Start-Process -FilePath $shortcut -WindowStyle Hidden
Start-Sleep -Seconds 4
$before=@(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object {$_.CommandLine -and $_.CommandLine.Contains($senderFile)})
if ($before.Count -lt 1) { throw 'Startup shortcut did not launch sender' }
Write-Output 'PASS: installed login shortcut launched the sender.'
$actual=$before | Where-Object {$_.ExecutablePath -notlike '*\.venv-windows\*'} | Select-Object -First 1
if (-not $actual) { throw 'Cannot identify exact sender process for restart test' }
Stop-Process -Id $actual.ProcessId -Force
Start-Sleep -Seconds 8
$after=@(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object {$_.CommandLine -and $_.CommandLine.Contains($senderFile)})
if (-not ($after | Where-Object {$_.ExecutablePath -notlike '*\.venv-windows\*' -and $_.ProcessId -ne $actual.ProcessId})) { throw 'Supervisor did not restart sender' }
Write-Output 'PASS: supervisor restarted sender after a forced unexpected exit.'
Get-Content (Join-Path $env:LOCALAPPDATA 'PCPanel\supervisor.log') -Tail 3
Get-Content (Join-Path $env:LOCALAPPDATA 'PCPanel\sender.log') -Tail 10
