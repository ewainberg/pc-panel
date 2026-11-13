# Windows telemetry sender

Installed on this PC in `C:\Users\elias\pc-panel`, from main revision
`bf66c286d6b7aa4cd5840cdb75d01b0f371d862d`.
The Linux sender and Pi receiver are unchanged. The Windows sender sends the
same 23 JSON keys as `host/rmine_telemetry.py`, as UTF-8 JSON over UDP.
The default destination is `10.6.25.9:7355`, at approximately 20 packets/second.

## Installed behavior

A shortcut named **PC Panel Telemetry** in the current user's Startup folder
runs `host/windows/run.ps1` hidden after login. It supervises `pythonw.exe` in
this user's interactive session and restarts it after an unexpected failure.
It is not a service, needs no administrator rights to run, and stores no password.
Keep this repository at its installed path, or rerun the installer after moving it.
The supervisor and sender each prevent duplicate instances for the current user.

The installation added Git for Windows and Python 3.12, plus an isolated
`.venv-windows` environment. It did not install a CPU sensor driver or change the
Pi or Linux installation. Windows Wi-Fi was reconnected to the already-saved
`Velocity Wi-Fi` profile because the Pi was unreachable while Wi-Fi was disconnected.
Ethernet alone was on another subnet. Leave Wi-Fi connected to reach this Pi.

## Configuration

Edit `%LOCALAPPDATA%\PCPanel\config.json` (on this PC,
`C:\Users\elias\AppData\Local\PCPanel\config.json`):

```json
{
  "target": "10.6.25.9",
  "port": 7355,
  "interval": 0.05,
  "audio": true,
  "media": true,
  "notifications": true
}
```

`interval` is seconds between packets; 0.05 means 20 Hz. Minimum is 0.01.
Restart after edits. Run these commands in ordinary PowerShell:

```powershell
cd C:\Users\elias\pc-panel
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\host\windows\control.ps1 -Stop
Start-Sleep -Seconds 3
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\host\windows\control.ps1 -Start
```

Stop persists until Start is used. The execution-policy override applies only
to those processes; the installer does not change the system execution policy.
Command-line `target`, `--port`, `--interval`, `--no-audio`, `--no-media`, and
`--no-notifications` override configuration values for manual runs.
Invalid configuration is reported to the supervisor log and retried after five seconds.

## Metrics and platform limits

- CPU/memory are percentages from psutil. Uptime is seconds since Windows boot;
  Windows Fast Startup can preserve the kernel boot time across shutdowns.
- `rx` and `tx` are aggregate download/upload rates in decimal **MB/s**, matching
  Linux, not Mbit/s. Rates clamp to zero if a counter resets.
- NVIDIA uses the first GPU returned by `nvidia-smi`, refreshed once a second.
  Usage and VRAM are percentages; temperature is Celsius. Missing values are
  JSON `null`, so the Pi can skip its GPU scene. NVIDIA tools run hidden with a timeout.
- CPU temperature is **0.0 on this PC**. Windows has no universal reliable CPU
  temperature API. If an existing LibreHardwareMonitor/OpenHardwareMonitor WMI
  provider exposes explicitly identified CPU temperature sensors, the sender
  uses their maximum reading in Celsius, checked every 30 seconds. ACPI thermal
  zones are deliberately not treated as CPU temperature. No privileged driver
  or hardware-monitor application was installed.
- Audio captures the **default WASAPI speaker/output loopback**, never a microphone.
  It uses the Linux sender's 25 ms windows, stereo level scaling, 32 linear
  frequency bands, bass range, and smoothing. Mono output is duplicated; for
  multichannel devices, left/right use the first two channels. The native sample
  rate is used. The default output endpoint is checked every second, with stream
  reconstruction on change/error. Missing callbacks produce zero values after
  0.75 seconds, rather than frozen gauges. If pycaw is missing, the stream is
  periodically reopened to discover default-device changes. Protected/exclusive
  output or some device drivers may not supply usable loopback audio.
- Media uses Windows GlobalSystemMediaTransportControlsSessionManager's current
  session. Only apps that publish Windows media-session metadata can supply tracks.
  Artist/title, position/duration in seconds, and `Playing`/`Paused`/`Stopped` are
  preserved. Position advances between player timeline updates while playing,
  freezes when paused, and is clamped to duration when available. Apps can omit
  artist or duration; a live stream can have duration zero. No current session
  sends empty strings and zeros. Temporary errors retain metadata for up to five
  seconds. `track_paused_for` and `audio_state` follow the Linux panel behavior.
- Track changes while playing create a four-second `music` notice, titled
  `NOW PLAYING`, when both artist and title exist. The initial track is not announced,
  matching Linux. New desktop notices take precedence.
- Notifications use Windows UserNotificationListener, polling once a second.
  Only new toast notifications visible to that API are forwarded, not the existing
  Action Center backlog. Some transient notifications can disappear before a poll;
  legacy balloon tips and app-private overlays may not be exposed. Notification
  IDs remain distinct across sender restarts. Denial or API failure affects only
  notifications and is retried; other metrics continue. Logs omit notification
  contents and track names. UDP packets themselves include these fields in plain
  text; set `notifications` to false if you do not want notification text forwarded.

## Notification permission

On this installation, Windows already reported **Allowed**, and a newly created
native test toast was successfully forwarded. No extra permission was needed.
Background startup never opens a permission prompt.

If access is denied or unset, explicitly open the foreground permission window:

```powershell
cd C:\Users\elias\pc-panel
.\.venv-windows\Scripts\python.exe .\host\rmine_telemetry_windows.py --request-notification-access
```

Click **Request access** and answer the Windows prompt. If previously denied,
Windows may require you to enable notification access under
**Settings > Privacy & security > Notifications**. Windows policy/builds can
require a packaged app declaring `userNotificationListener`; the window explains
if an unpackaged Python sender cannot obtain permission. Packaging/signing such
a helper is not part of this installation. Other telemetry works without it.
See [Microsoft's notification-listener requirements](https://learn.microsoft.com/en-us/windows/apps/develop/notifications/app-notifications/notification-listener).

## Status, logs, troubleshooting

```powershell
cd C:\Users\elias\pc-panel
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\host\windows\control.ps1 -Status
Get-Content "$env:LOCALAPPDATA\PCPanel\sender.log" -Tail 30 -Wait
```

Logs: `%LOCALAPPDATA%\PCPanel\sender.log` (2 MB, three backups) and
`supervisor.log` (rotated at supervisor startup once over 1 MB).
State transitions report available/missing components. A heartbeat once a minute
reports sent datagrams and send errors. A successful UDP send does not prove delivery.
The Pi's `PC NO DATA` indicator should disappear once packets arrive; it returns
about 2.5 seconds after incoming telemetry stops, provided the Pi's Wi-Fi is connected.
Check `ping 10.6.25.9` and Wi-Fi connectivity if it stays visible.

For a foreground diagnostic run, first stop the installed sender and wait three
seconds, remove its stop flag, then run:

```powershell
Remove-Item "$env:LOCALAPPDATA\PCPanel\stop.flag" -ErrorAction SilentlyContinue
.\.venv-windows\Scripts\python.exe .\host\rmine_telemetry_windows.py 10.6.25.9 --duration 10
```

Afterward, use `control.ps1 -Start` to restore supervised operation. Optional
`--dump output.json` writes the final packet, which may contain private metadata.

## Tests

Stop the installed sender, wait three seconds, and remove the stop flag before
running tests, since only one sender is allowed per user.

```powershell
.\.venv-windows\Scripts\python.exe .\host\windows\test_sender.py
.\.venv-windows\Scripts\python.exe .\host\windows\test_native_windows.py
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\host\windows\test_startup.ps1
```

The first test plays a quiet two-second stereo tone and validates all packets
received by a local UDP socket. It also compares DSP output against the actual
Linux class and exercises the actual Pi receiver class without hardware imports.
The native test publishes temporary test media metadata and one test toast,
then removes its toast. The startup test stops/starts the installed sender and
forces one unexpected sender exit to confirm automatic recovery; it leaves the
sender running. See `WINDOWS-VERIFICATION.md` for the installed PC's results.

## Reinstall on another Windows PC

Install 64-bit Python 3.12 and Git, clone main, then:

```powershell
cd C:\path\to\pc-panel
py -3.12 -m venv .venv-windows
.\.venv-windows\Scripts\python.exe -m pip install -r .\host\windows\requirements.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\host\windows\install.ps1 -Target 10.6.25.9 -Port 7355
```

The installer preserves an existing config. Change it directly if reinstalling.
The requirements file pins the tested environment, including optional packages
used by the native test. Core CPU/memory/network/uptime require psutil; missing
audio or WinRT packages disable only their respective optional features.

## Removal

```powershell
cd C:\Users\elias\pc-panel
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\host\windows\uninstall.ps1
```

This stops the processes and removes only **PC Panel Telemetry.lnk** from the
current user's Startup folder. Source, config, and logs are retained. After a few
seconds you may delete `C:\Users\elias\pc-panel` and
`C:\Users\elias\AppData\Local\PCPanel` in Explorer if you want a complete removal.
Git and Python remain installed; uninstall them through Windows Installed Apps
only if you no longer need them. No Pi/Linux removal steps are necessary.

## Terminal shortcut

The installed `panel` command opens the original terminal controller, targeting
10.6.25.9 by default. Use `panel OTHER_IP` to choose another Pi. To recreate the
shortcut on this Windows account, copy the included command wrapper to the
existing user command-path directory:

```powershell
Copy-Item .\host\windows\panel.cmd "$env:LOCALAPPDATA\Microsoft\WinGet\Links\panel.cmd"
panel
```

The wrapper expects this checkout at `%USERPROFILE%\pc-panel`. Remove that
`panel.cmd` file when removing the terminal shortcut. The controller uses
`windows-curses`, included in the Windows requirements.
