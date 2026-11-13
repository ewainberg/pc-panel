# Hidden per-user watchdog. Startup-folder shortcut runs in the logged-on session.
$ErrorActionPreference = 'Stop'
$repoDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$stateDir = Join-Path $env:LOCALAPPDATA 'PCPanel'
$pythonExe = Join-Path $repoDir '.venv-windows\Scripts\pythonw.exe'
$senderFile = Join-Path $repoDir 'host\rmine_telemetry_windows.py'
$configFile = Join-Path $stateDir 'config.json'
$stopFile = Join-Path $stateDir 'stop.flag'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$guard = $null
$child = $null
try {
    try { $guard = [IO.File]::Open((Join-Path $stateDir 'supervisor.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch [IO.IOException] { exit 0 }
    Set-Content -LiteralPath (Join-Path $stateDir 'supervisor.pid') -Value $PID
    $supervisorLog = Join-Path $stateDir 'supervisor.log'
    if ((Test-Path $supervisorLog) -and (Get-Item $supervisorLog).Length -gt 1MB) {
        Move-Item -LiteralPath $supervisorLog -Destination ($supervisorLog + '.1') -Force
    }
    while (-not (Test-Path -LiteralPath $stopFile)) {
        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = $pythonExe
        $startInfo.Arguments = '"' + $senderFile + '" --config "' + $configFile + '"'
        $startInfo.WorkingDirectory = $repoDir
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $child = [Diagnostics.Process]::Start($startInfo)
        Set-Content -LiteralPath (Join-Path $stateDir 'sender.pid') -Value $child.Id
        while (-not $child.WaitForExit(1000)) {
            if (Test-Path -LiteralPath $stopFile) {
                # Sender watches stop.flag and exits cleanly, including the venv child.
                if (-not $child.WaitForExit(5000)) {
                    & "$env:SystemRoot\System32\taskkill.exe" /PID $child.Id /T /F | Out-Null
                    $child.WaitForExit()
                }
                break
            }
        }
        $code = $child.ExitCode
        $child.Dispose()
        $child = $null
        Remove-Item -LiteralPath (Join-Path $stateDir 'sender.pid') -ErrorAction SilentlyContinue
        if ($code -eq 0 -or (Test-Path -LiteralPath $stopFile)) { break }
        Add-Content -LiteralPath $supervisorLog -Value ((Get-Date -Format o) + ' Sender exited ' + $code + '; restarting in 5 seconds.')
        Start-Sleep -Seconds 5
    }
} catch {
    Add-Content -LiteralPath (Join-Path $stateDir 'supervisor.log') -Value ((Get-Date -Format o) + ' ' + $_.Exception.Message)
} finally {
    if ($child -and -not $child.HasExited) { & "$env:SystemRoot\System32\taskkill.exe" /PID $child.Id /T /F | Out-Null; $child.Dispose() }
    if ($guard) {
        Remove-Item -LiteralPath (Join-Path $stateDir 'sender.pid') -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $stateDir 'supervisor.pid') -ErrorAction SilentlyContinue
        $guard.Dispose()
    }
}
