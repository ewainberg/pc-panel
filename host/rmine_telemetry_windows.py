#!/usr/bin/env python3
"""Interactive Windows sender for the unchanged rmine panel UDP protocol."""
import argparse
import asyncio
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

import psutil

LOG = logging.getLogger("pc-panel")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
EMPTY_TRACK = ("", "", 0.0, 0.0, "")
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PCPanel"


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def percent(value):
    return max(0.0, min(100.0, number(value)))


class Health:
    """Log state transitions, without recording track or notification contents."""
    def __init__(self):
        self.states = {}
        self.lock = threading.Lock()

    def set(self, component, state):
        with self.lock:
            if self.states.get(component) != state:
                self.states[component] = state
                LOG.info("%s: %s", component, state)


class AudioDSP:
    """Same window, linear bands, scaling and smoothing as the Linux sender."""
    def __init__(self, rate=48000):
        import numpy as np
        self.np = np
        self.rate = rate
        self.levels = (0.0, 0.0)
        self.bands = [0.0] * 32
        self.bass = self.overall = 0.0

    def feed(self, samples):
        np = self.np
        samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)
        if samples.shape[1] == 1:
            samples = np.repeat(samples, 2, axis=1)
        stereo = samples[:, :2]
        rms = np.sqrt(np.mean(np.square(stereo), axis=0))
        db = 20.0 * np.log10(np.maximum(rms, 1e-6))
        targets = [percent((v + 55.0) * 100.0 / 45.0) for v in db]
        self.levels = tuple(c + (t - c) * (0.5 if t > c else 0.12)
                            for c, t in zip(self.levels, targets))
        mono = np.mean(stereo, axis=1)
        window = np.hanning(len(mono))
        spectrum = np.abs(np.fft.rfft(mono * window))[2:1026] / max(np.sum(window) / 2.0, 1.0)
        frequencies = np.fft.rfftfreq(len(mono), 1.0 / self.rate)[2:1026]
        peaks = [float(np.max(b)) if len(b) else 0.0 for b in np.array_split(spectrum, 32)]
        db = 20.0 * np.log10(np.maximum(peaks, 1e-6))
        targets = [percent((v + 75.0) * 100.0 / 40.0) for v in db]
        self.bands = [c + (t - c) * (0.55 if t > c else 0.1)
                      for c, t in zip(self.bands, targets)]
        bins = spectrum[(frequencies >= 20.0) & (frequencies <= 250.0)]
        bass_rms = float(np.sqrt(np.mean(np.square(bins)))) if len(bins) else 0.0
        target = percent((20.0 * math.log10(max(bass_rms, 1e-6)) + 70.0) * 100.0 / 50.0)
        self.bass += (target - self.bass) * (0.35 if target > self.bass else 0.18)
        target = max(self.levels)
        self.overall += (target - self.overall) * (0.5 if target > self.overall else 0.12)
        return (self.levels, self.bands[:], float(self.bass), float(self.overall))


class AudioMonitor:
    def __init__(self, stop, health, enabled=True):
        self.stop, self.health = stop, health
        self.lock = threading.Lock()
        self.value = ((0.0, 0.0), [0.0] * 32, 0.0, 0.0)
        self.updated = 0.0
        if enabled:
            threading.Thread(target=self.run, name="audio", daemon=True).start()
        else:
            health.set("audio", "disabled")

    def snapshot(self):
        with self.lock:
            if time.monotonic() - self.updated > 0.75:
                return ((0.0, 0.0), [0.0] * 32, 0.0, 0.0)
            return self.value

    def run(self):
        try:
            import numpy as np
            import pyaudiowpatch as pa
        except ImportError as e:
            self.health.set("audio", f"optional dependency missing: {e.name}")
            return
        try:
            import comtypes
            comtypes.CoInitialize()
            from pycaw.pycaw import AudioUtilities
            def endpoint():
                return AudioUtilities.GetSpeakers().id
        except Exception:
            endpoint = None
            self.health.set("audio-device-watch", "pycaw unavailable; periodically reopening output")
        try:
            while not self.stop.is_set():
                p = stream = None
                try:
                    device_id = endpoint() if endpoint else None
                    p = pa.PyAudio()
                    device = p.get_default_wasapi_loopback()
                    # Never fall back to an input device: only an explicit WASAPI loopback.
                    if not device.get("isLoopbackDevice"):
                        raise RuntimeError("default output has no WASAPI loopback")
                    channels = int(device["maxInputChannels"])
                    rate = int(device["defaultSampleRate"])
                    if channels < 1 or rate < 8000:
                        raise RuntimeError("invalid output audio format")
                    frames = max(64, int(rate * 0.025))
                    dsp = AudioDSP(rate)
                    pending = np.empty((0, channels), dtype=np.float32)
                    def callback(data, frame_count, time_info, status):
                        nonlocal pending
                        try:
                            block = np.frombuffer(data, dtype=np.float32).reshape(-1, channels)
                            pending = np.concatenate((pending, block))
                            while len(pending) >= frames:
                                value = dsp.feed(pending[:frames])
                                pending = pending[frames:]
                                with self.lock:
                                    self.value, self.updated = value, time.monotonic()
                            return (None, pa.paContinue)
                        except Exception:
                            return (None, pa.paAbort)
                    stream = p.open(format=pa.paFloat32, channels=channels, rate=rate,
                                    input=True, input_device_index=device["index"],
                                    frames_per_buffer=frames, stream_callback=callback)
                    self.health.set("audio", "capturing output: " + device["name"])
                    opened = time.monotonic()
                    while not self.stop.wait(1.0):
                        if not stream.is_active():
                            raise RuntimeError("loopback stream stopped")
                        if endpoint and endpoint() != device_id:
                            self.health.set("audio", "default output changed; reopening")
                            break
                        if not endpoint and time.monotonic() - opened > 15.0:
                            break
                except Exception as e:
                    self.health.set("audio", f"unavailable; retrying ({type(e).__name__})")
                finally:
                    if stream:
                        try:
                            stream.close()
                        except Exception:
                            pass
                    if p:
                        p.terminate()
                    with self.lock:
                        self.updated = 0.0
                self.stop.wait(1.0)
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


class Poller:
    def __init__(self, name, function, default, interval, stop, health):
        self.value = default
        self.updated = 0.0
        self.default, self.interval = default, interval
        self.function, self.stop, self.health, self.name = function, stop, health, name
        threading.Thread(target=self.run, name=name, daemon=True).start()

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.value = self.function()
                self.updated = time.monotonic()
                self.health.set(self.name, "available" if self.value != self.default else "unavailable")
            except Exception as e:
                self.value = self.default
                self.health.set(self.name, f"unavailable ({type(e).__name__})")
            self.stop.wait(max(0.1, self.interval - (time.monotonic() - started)))

    def snapshot(self):
        return self.value if time.monotonic() - self.updated < max(5.0, self.interval * 2) else self.default


def gpu_stats():
    executable = shutil.which("nvidia-smi")
    if not executable:
        candidate = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/nvidia-smi.exe"
        if candidate.exists():
            executable = str(candidate)
    if not executable:
        return (None, None, None)
    result = subprocess.check_output(
        [executable, "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
         "--format=csv,noheader,nounits"], text=True, timeout=2.0,
        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW).splitlines()[0]
    util, temp, used, total = [number(v.strip(), None) for v in result.split(",")]
    return (percent(util) if util is not None else None, temp,
            percent(used / total * 100.0) if used is not None and total and total > 0 else None)


def cpu_temp():
    # Only explicitly identified CPU sensors from an existing hardware-monitor
    # provider. ACPI thermal-zone readings are not necessarily CPU temperatures.
    script = """$ErrorActionPreference='SilentlyContinue';
    foreach ($ns in @('root/LibreHardwareMonitor','root/OpenHardwareMonitor')) {
      Get-CimInstance -Namespace $ns -ClassName Sensor | Where-Object {
        $_.SensorType -eq 'Temperature' -and $_.Identifier -match '/(intelcpu|amdcpu|cpu)/'
      } | ForEach-Object { $_.Value.ToString([Globalization.CultureInfo]::InvariantCulture) }
    }"""
    output = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        text=True, timeout=4, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW).stdout
    values = [number(v) for v in output.splitlines()]
    values = [v for v in values if 0.0 < v < 130.0]
    return max(values) if values else 0.0


class MediaMonitor:
    def __init__(self, stop, health, enabled=True):
        self.stop, self.health = stop, health
        self.value = EMPTY_TRACK
        self.updated = 0.0
        self.lock = threading.Lock()
        if enabled:
            threading.Thread(target=self.run, name="media", daemon=True).start()
        else:
            health.set("media", "disabled")

    def snapshot(self):
        with self.lock:
            return self.value if time.monotonic() - self.updated < 5.0 else EMPTY_TRACK

    def run(self):
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status
        except ImportError as e:
            self.health.set("media", f"optional dependency missing: {e.name}")
            return

        async def monitor():
            manager = None
            while not self.stop.is_set():
                try:
                    if manager is None:
                        manager = await asyncio.wait_for(Manager.request_async(), 3.0)
                    session = manager.get_current_session()
                    track = EMPTY_TRACK
                    if session:
                        props = await asyncio.wait_for(session.try_get_media_properties_async(), 2.0)
                        playback = session.get_playback_info()
                        timeline = session.get_timeline_properties()
                        status = {Status.PLAYING: "Playing", Status.PAUSED: "Paused",
                                  Status.STOPPED: "Stopped"}.get(playback.playback_status, "")
                        start = timeline.start_time.total_seconds()
                        duration = max(0.0, timeline.end_time.total_seconds() - start)
                        position = max(0.0, timeline.position.total_seconds() - start)
                        # Windows players often update timelines only on seek/pause.
                        if status == "Playing":
                            age = (datetime.now(timezone.utc) - timeline.last_updated_time).total_seconds()
                            position += max(0.0, age) * max(0.0, number(playback.playback_rate, 1.0))
                        if duration:
                            position = min(position, duration)
                        track = (props.artist.strip(), props.title.strip(), number(position), number(duration), status)
                    with self.lock:
                        self.value, self.updated = track, time.monotonic()
                    self.health.set("media", "session available" if session else "ready; no active session")
                except Exception as e:
                    manager = None
                    self.health.set("media", f"temporary failure ({type(e).__name__}); retrying")
                await asyncio.sleep(0.5)
        asyncio.run(monitor())


class NotificationMonitor:
    def __init__(self, stop, health, enabled=True):
        self.events = deque(maxlen=8)
        self.lock = threading.Lock()
        self.stop, self.health = stop, health
        if enabled:
            threading.Thread(target=self.run, name="notifications", daemon=True).start()
        else:
            health.set("notifications", "disabled")

    def pop(self):
        with self.lock:
            return self.events.popleft() if self.events else None

    def run(self):
        try:
            from winrt.windows.ui.notifications.management import UserNotificationListener, UserNotificationListenerAccessStatus as Access
            from winrt.windows.ui.notifications import NotificationKinds, KnownNotificationBindings
        except ImportError as e:
            self.health.set("notifications", f"optional dependency missing: {e.name}")
            return
        async def monitor():
            seen = None
            while not self.stop.is_set():
                try:
                    listener = UserNotificationListener.current
                    if listener.get_access_status() != Access.ALLOWED:
                        self.health.set("notifications", "permission denied/unset; use --request-notification-access")
                        seen = None
                        await asyncio.sleep(10.0)
                        continue
                    items = await asyncio.wait_for(listener.get_notifications_async(NotificationKinds.TOAST), 3.0)
                    current = set()
                    events = []
                    for item in sorted(items, key=lambda n: n.creation_time):
                        key = (item.id, item.creation_time.isoformat())
                        current.add(key)
                        if seen is None or key in seen:
                            continue  # Do not replay the existing Action Center backlog.
                        try:
                            binding = item.notification.visual.get_binding(KnownNotificationBindings.toast_generic)
                            if not binding:
                                continue
                            texts = [" ".join(t.text.split()) for t in binding.get_text_elements()]
                            if texts and texts[0]:
                                events.append((texts[0][:512], " ".join(texts[1:])[:2048]))
                        except Exception:
                            continue
                    seen = current
                    with self.lock:
                        self.events.extend(events)
                    self.health.set("notifications", "listening; existing backlog suppressed")
                except Exception as e:
                    self.health.set("notifications", f"unavailable ({type(e).__name__}); retrying")
                await asyncio.sleep(1.0)
        asyncio.run(monitor())


def request_notification_access():
    """Explicit foreground permission flow; never invoked by background startup."""
    # RequestAccessAsync requires a UI thread. Provide a Tk window and pump the
    # asyncio STA operation from its UI loop. Some Windows builds require package
    # identity/capability; report this without affecting normal telemetry.
    sys.coinit_flags = 2
    import tkinter as tk
    import comtypes
    comtypes.CoInitializeEx(2)
    from winrt.windows.ui.notifications.management import UserNotificationListener
    root = tk.Tk()
    root.title("PC Panel notification permission")
    tk.Label(root, text="Allow PC Panel to forward new Windows notifications\nto your Raspberry Pi over the local network.", padx=24, pady=20).pack()
    result = tk.StringVar(value="Click Request access to open the Windows permission prompt.")
    tk.Label(root, textvariable=result, wraplength=460, padx=20, pady=12).pack()
    loop = asyncio.new_event_loop()
    async def request():
        try:
            status = await UserNotificationListener.current.request_access_async()
            result.set("Windows notification access: " + status.name +
                       ". If denied, enable access in Windows Settings > Privacy & security > Notifications.")
        except Exception as e:
            result.set(f"Windows could not grant access ({type(e).__name__}). This Windows build may require a packaged helper with the userNotificationListener capability. Other telemetry still works.")
    def start():
        loop.create_task(request())
    tk.Button(root, text="Request access", command=start).pack(pady=8)
    tk.Button(root, text="Close", command=root.destroy).pack(pady=8)
    def pump():
        loop.call_soon(loop.stop)
        loop.run_forever()
        root.after(20, pump)
    root.after(20, pump)
    root.mainloop()
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    comtypes.CoUninitialize()


class PanelState:
    def __init__(self):
        self.track_key = self.pause_key = None
        self.pause_started = 0.0
        self.audio_last_active = -math.inf
        self.notice = None
        # IDs must change across sender restarts while the Pi stays powered on.
        self.notice_id = time.time_ns() // 1_000_000
        self.notice_until = 0.0

    def update(self, now, track, audio, desktop_notice=None):
        artist, title, position, duration, status = track
        levels, bands, bass, overall = audio
        key = (artist, title)
        if status == "Playing" and artist and title and self.track_key is not None and key != self.track_key:
            self.notice_id += 1
            self.notice = {"id": self.notice_id, "kind": "music", "title": "NOW PLAYING", "message": f"{artist} - {title}"}
            self.notice_until = now + 4.0
        if track != EMPTY_TRACK:
            self.track_key = key
        if desktop_notice:
            self.notice_id += 1
            self.notice = {"id": self.notice_id, "kind": "system", "title": desktop_notice[0], "message": desktop_notice[1]}
            self.notice_until = now + 4.0
        if now >= self.notice_until:
            self.notice = None
        if status == "Playing":
            audio_state = "playing"
        elif status == "Paused":
            audio_state = "paused"
        else:
            if overall >= 12.0:
                self.audio_last_active = now
            audio_state = "playing" if now - self.audio_last_active < 3.0 else "standby"
        pause_key = key if status == "Paused" else None
        if pause_key != self.pause_key:
            self.pause_key, self.pause_started = pause_key, now
        return {
            "audio_left": float(levels[0]), "audio_right": float(levels[1]),
            "audio_bands": bands, "audio_bass": float(bass), "audio_overall": float(overall),
            "track_artist": artist, "track_title": title, "track_position": float(position),
            "track_duration": float(duration), "track_status": status,
            "track_paused_for": max(0.0, now - self.pause_started) if pause_key else 0.0,
            "audio_state": audio_state, "notification": self.notice,
        }


class Sender:
    def __init__(self, args):
        self.stop = threading.Event()
        self.health = Health()
        self.audio = AudioMonitor(self.stop, self.health, args.audio)
        self.media = MediaMonitor(self.stop, self.health, args.media)
        self.notifications = NotificationMonitor(self.stop, self.health, args.notifications)
        self.gpu = Poller("gpu", gpu_stats, (None, None, None), 1.0, self.stop, self.health)
        self.temp = Poller("cpu-temperature", cpu_temp, 0.0, 30.0, self.stop, self.health)
        self.state = PanelState()
        self.prev_net, self.prev_t = psutil.net_io_counters(), time.monotonic()
        self.hostname = socket.gethostname()
        self.boot_time = psutil.boot_time()
        psutil.cpu_percent(None)

    def sample(self):
        now = time.monotonic()
        rx = tx = 0.0
        try:
            net = psutil.net_io_counters()
            dt = max(now - self.prev_t, 0.001)
            rx = max(0.0, (net.bytes_recv - self.prev_net.bytes_recv) / dt / 1_000_000)
            tx = max(0.0, (net.bytes_sent - self.prev_net.bytes_sent) / dt / 1_000_000)
            self.prev_net, self.prev_t = net, now
        except Exception:
            pass
        try:
            cpu, mem = percent(psutil.cpu_percent(None)), percent(psutil.virtual_memory().percent)
        except Exception:
            cpu = mem = 0.0
        gu, gt, gm = self.gpu.snapshot()
        packet = {"hostname": self.hostname, "cpu": cpu, "mem": mem,
                  "cpu_temp": number(self.temp.snapshot()), "rx": rx, "tx": tx,
                  "uptime": max(0.0, time.time() - self.boot_time),
                  "gpu_util": gu, "gpu_temp": gt, "gpu_mem": gm}
        packet.update(self.state.update(now, self.media.snapshot(), self.audio.snapshot(), self.notifications.pop()))
        return packet


def load_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="Pi address; overrides config")
    parser.add_argument("--config", type=Path, default=DATA_DIR / "config.json")
    parser.add_argument("--port", type=int)
    parser.add_argument("--interval", type=float, help="seconds between packets (default 0.05)")
    for feature in ("audio", "media", "notifications"):
        parser.add_argument("--" + feature, action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--duration", type=float, default=0, help="exit after N seconds (0: continuous)")
    parser.add_argument("--dump", type=Path, help="write final packet for diagnostics; may contain private metadata")
    parser.add_argument("--request-notification-access", action="store_true")
    args = parser.parse_args()
    config = {}
    if args.config.exists():
        try:
            config = json.loads(args.config.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict):
                raise ValueError("config must be a JSON object")
        except (OSError, ValueError) as e:
            parser.error(f"cannot read config: {e}")
    args.target = args.target or config.get("target", "10.6.25.9")
    args.port = args.port if args.port is not None else config.get("port", 7355)
    args.interval = args.interval if args.interval is not None else config.get("interval", 0.05)
    for feature in ("audio", "media", "notifications"):
        if getattr(args, feature) is None:
            setattr(args, feature, config.get(feature, True))
        if not isinstance(getattr(args, feature), bool):
            parser.error(feature + " must be true or false")
    if not isinstance(args.target, str) or not args.target:
        parser.error("target must be a hostname or IP")
    if type(args.port) is not int or not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if not isinstance(args.interval, (int, float)) or not math.isfinite(args.interval) or args.interval < 0.01:
        parser.error("interval must be finite and at least 0.01 seconds")
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error("duration must be finite and nonnegative")
    return args


def single_instance():
    import msvcrt
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = (DATA_DIR / "sender.lock").open("a+b")
    handle.seek(0)
    if not handle.read(1):
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def main():
    args = load_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_handler = RotatingFileHandler(DATA_DIR / "sender.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handlers = [log_handler]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if args.request_notification_access:
        request_notification_access()
        return
    lock = single_instance()
    if lock is None:
        LOG.info("Another sender is already running for this user; exiting")
        return
    sender = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.2)
    try:
        sender = Sender(args)
        LOG.info("Started PID %s; target %s:%s; interval %.3fs", os.getpid(), args.target, args.port, args.interval)
        started = deadline = time.monotonic()
        resolve_at, destination = 0.0, None
        sent = errors = 0
        last_report = started
        packet = None
        while (not args.duration or time.monotonic() - started < args.duration) and not (DATA_DIR / "stop.flag").exists():
            now = time.monotonic()
            if now >= resolve_at:
                try:
                    destination = (socket.gethostbyname(args.target), args.port)
                    sender.health.set("destination", "resolved")
                    resolve_at = now + 60.0
                except OSError:
                    sender.health.set("destination", "DNS unavailable; retrying")
                    resolve_at = now + 5.0
            packet = sender.sample()
            if destination:
                try:
                    payload = json.dumps(packet, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
                    # Pi receives at most 8192 bytes. Bound only text if needed.
                    if len(payload) > 8000:
                        packet["track_artist"] = packet["track_artist"][:128]
                        packet["track_title"] = packet["track_title"][:256]
                        if packet["notification"]:
                            packet["notification"] = dict(packet["notification"], title=packet["notification"]["title"][:128], message=packet["notification"]["message"][:512])
                        payload = json.dumps(packet, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
                    sock.sendto(payload, destination)
                    sent += 1
                    sender.health.set("udp", "sending (delivery not acknowledged)")
                except (OSError, ValueError, TypeError) as e:
                    errors += 1
                    sender.health.set("udp", f"send failed ({type(e).__name__}); retrying")
            if now - last_report >= 60.0:
                LOG.info("Heartbeat: %s datagrams sent, %s send errors", sent, errors)
                last_report = now
            deadline += args.interval
            if deadline < time.monotonic() - args.interval:
                deadline = time.monotonic()
            sender.stop.wait(max(0.0, deadline - time.monotonic()))
        if args.dump and packet:
            args.dump.write_text(json.dumps(packet, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        LOG.info("Finished: %s datagrams sent, %s send errors", sent, errors)
    except KeyboardInterrupt:
        LOG.info("Stopped by user")
    finally:
        if sender:
            sender.stop.set()
        sock.close()
        lock.close()


if __name__ == "__main__":
    main()
