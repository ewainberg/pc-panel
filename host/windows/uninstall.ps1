$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'control.ps1') -Stop
$shortcutFile = Join-Path ([Environment]::GetFolderPath('Startup')) 'PC Panel Telemetry.lnk'
Remove-Item -LiteralPath $shortcutFile -ErrorAction SilentlyContinue
Write-Output 'Removed PC Panel login startup. Source, isolated Python environment, configuration and logs are retained.'
