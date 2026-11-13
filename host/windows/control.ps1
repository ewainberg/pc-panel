param([switch]$Stop, [switch]$Start, [switch]$Status)
$ErrorActionPreference = 'Stop'
$stateDir = Join-Path $env:LOCALAPPDATA 'PCPanel'
$launcher = Join-Path $PSScriptRoot 'run.ps1'
if ($Stop) {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $stateDir 'stop.flag') -Value 'stop'
    Write-Output 'Stop requested. The sender and supervisor will exit within a few seconds.'
} elseif ($Start) {
    Remove-Item -LiteralPath (Join-Path $stateDir 'stop.flag') -ErrorAction SilentlyContinue
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',('"' + $launcher + '"')) -WindowStyle Hidden
    Write-Output 'Started per-user supervisor. Check status and sender.log.'
} else {
    foreach ($name in @('supervisor','sender')) {
        $pidFile = Join-Path $stateDir ($name + '.pid')
        $processInfo = $null
        if (Test-Path -LiteralPath $pidFile) {
            $processNumber = [int](Get-Content -LiteralPath $pidFile)
            $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$processNumber" -ErrorAction SilentlyContinue
        }
        if ($processInfo) {
            Write-Output ($name + ': PID ' + $processInfo.ProcessId + ' ' + $processInfo.CommandLine)
        } else { Write-Output ($name + ': not running') }
    }
    Get-Content -LiteralPath (Join-Path $stateDir 'sender.log') -Tail 12 -ErrorAction SilentlyContinue
}
