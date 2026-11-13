# Windows verification — 2026-11-13

Reference: main `bf66c286d6b7aa4cd5840cdb75d01b0f371d862d`.
Installed as Windows user `elias`, Windows 10.0.26300.0, Python 3.12.10.
GPU: NVIDIA GeForce RTX 5080. Default output: Realtek HD Audio 2nd output,
48 kHz stereo WASAPI loopback.

## Passed

- Six automated tests: exact Linux audio DSP parity, track/pause/notice behavior,
  missing optional audio dependency, unavailable GPU fields, sensor failure
  isolation, and the actual Pi receiver's UTF-8/HOST handling and stale fallback.
- UDP integration: 160 datagrams received locally, 20.03 Hz; every packet matched
  all 23 Linux JSON keys, expected types, finite numeric ranges, 32 audio bands,
  and the receiver's packet-size limit.
- A two-second quiet stereo output tone produced distinct left/right levels,
  frequency bands and bass; maximum overall level 61.22. Capture device was
  explicitly marked WASAPI loopback. No microphone capture path exists.
- NVIDIA readings present. CPU, memory, uptime and network-rate collection ran
  during integration. No send errors during the eight-second test.
- Native Windows media-session integration: test artist/title, 180-second
  duration, advancing playing position, stable paused position, increasing
  paused age, and track-change music notification all received over UDP.
- Native Windows toast integration: API access Allowed, initial backlog suppressed,
  newly generated test toast title/body received over UDP. Test toast removed.
- Pi reachability restored by reconnecting existing Windows Wi-Fi profile.
  Ping received 2/2 replies and neighbor resolved to B8-27-EB-25-4F-90.
- Production sender running at 10.6.25.9:7355, interval 0.05.
- User physically confirmed the panel now shows PC telemetry and PC NO DATA
  disappeared. Pi and Linux installations were not changed.
- Per-user Startup shortcut launched successfully when invoked directly.
- Stop removed the full virtual-environment process tree.
- Supervisor restarted the sender after a forced unexpected exit, with a new
  sender process after the configured five-second restart delay.

## Not verified / unavailable

- CPU temperature unavailable: no reliable CPU hardware-monitor WMI provider;
  sender emits 0.0, matching Linux's unavailable fallback.
- Actual sign-out/sign-in or reboot startup has not been performed. Startup
  shortcut invocation and current interactive-session operation were tested.
- Live output-device unplug/switch, Windows notification permission denial UI,
  and permission-grant dialog on an account without access were not exercised.
- Media testing used a real synthetic Windows media session, not every player's
  implementation. Apps that do not publish media-session metadata cannot be read.
- Physical audio gauge/spectrum motion and on-panel track/desktop notice visuals
  were not separately confirmed by the user; their packet contents were tested.
- No packet capture or receiver logs on the Pi were inspected, because this task
  leaves the Pi installation alone. Physical telemetry observation confirms delivery.
