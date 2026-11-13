param([string]$Target = '10.6.25.9', [int]$Port = 7355, [double]$Interval = 0.05)
$ErrorActionPreference = 'Stop'
$repoDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$pythonExe = Join-Path $repoDir '.venv-windows\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Create .venv-windows and install host/windows/requirements.txt first. See README-Windows.md.' }
if ($Port -lt 1 -or $Port -gt 65535 -or $Interval -lt 0.01 -or [double]::IsNaN($Interval) -or [double]::IsInfinity($Interval)) { throw 'Invalid port or interval' }
$stateDir = Join-Path $env:LOCALAPPDATA 'PCPanel'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$configFile = Join-Path $stateDir 'config.json'
if (-not (Test-Path -LiteralPath $configFile)) {
    @{ target=$Target; port=$Port; interval=$Interval; audio=$true; media=$true; notifications=$true } | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding UTF8
}
$shortcutFile = Join-Path ([Environment]::GetFolderPath('Startup')) 'PC Panel Telemetry.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutFile)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shortcut.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $PSScriptRoot 'run.ps1') + '"'
$shortcut.WorkingDirectory = $repoDir
$shortcut.WindowStyle = 7
$shortcut.Description = 'PC Panel telemetry in the logged-on Windows user session'
$shortcut.Save()
& (Join-Path $PSScriptRoot 'control.ps1') -Start
Write-Output ('Startup installed: ' + $shortcutFile)
Write-Output ('Configuration: ' + $configFile)
