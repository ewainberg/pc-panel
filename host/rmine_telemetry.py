#!/usr/bin/env python3
import argparse
import ast
import json
import socket
import subprocess
import threading
import time
from collections import deque

import numpy as np
import psutil


class AudioMonitor:
    def __init__(self, source=None):
        self.levels = (0.0, 0.0)
        self.bands = [0.0] * 32
        self.bass = 0.0
        self.overall = 0.0
        source = source or self.default_monitor()
        if not source:
            return
        try:
            self.process = subprocess.Popen(
                ["parec", "--raw", "--format=s16le", "--rate=48000", "--channels=2", "--latency-msec=10", "--process-time-msec=10", "--device", source],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            threading.Thread(target=self.read_levels, daemon=True).start()
        except OSError:
            pass

    @staticmethod
    def default_monitor():
        try:
            sink = subprocess.check_output(["pactl", "get-default-sink"], text=True).strip()
            return f"{sink}.monitor"
        except Exception:
            return None

    def read_levels(self):
        while True:
            data = self.process.stdout.read(1200 * 4)
            if len(data) != 1200 * 4:
                return
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
            rms = np.sqrt(np.mean(np.square(samples), axis=0)) / 32768.0
            db = 20.0 * np.log10(np.maximum(rms, 1e-6))
            targets = [float(max(0.0, min(100.0, (value + 55.0) * 100.0 / 45.0))) for value in db]
            self.levels = tuple(
                current + (target - current) * (0.5 if target > current else 0.12)
                for current, target in zip(self.levels, targets)
            )
            mono = np.mean(samples, axis=1) / 32768.0
            window = np.hanning(len(mono))
            spectrum = np.abs(np.fft.rfft(mono * window))[2:1026] / max(np.sum(window) / 2.0, 1.0)
            frequencies = np.fft.rfftfreq(len(mono), 1.0 / 48000.0)[2:1026]
            band_peaks = [np.max(band) for band in np.array_split(spectrum, 32)]
            band_db = 20.0 * np.log10(np.maximum(band_peaks, 1e-6))
            targets = [float(max(0.0, min(100.0, (value + 75.0) * 100.0 / 40.0))) for value in band_db]
            self.bands = [
                current + (target - current) * (0.55 if target > current else 0.1)
                for current, target in zip(self.bands, targets)
            ]
            bass_bins = spectrum[(frequencies >= 20.0) & (frequencies <= 250.0)]
            bass_rms = np.sqrt(np.mean(np.square(bass_bins)))
            bass_target = float(max(0.0, min(100.0, (20.0 * np.log10(max(bass_rms, 1e-6)) + 70.0) * 100.0 / 50.0)))
            overall_target = max(self.levels)
            self.bass += (bass_target - self.bass) * (0.35 if bass_target > self.bass else 0.18)
            self.overall += (overall_target - self.overall) * (0.5 if overall_target > self.overall else 0.12)


class NotificationMonitor:
    def __init__(self):
        self.events = deque(maxlen=8)
        self.lock = threading.Lock()
        try:
            self.process = subprocess.Popen(
                ["dbus-monitor", "--session", "interface='org.freedesktop.Notifications',member='Notify'"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            threading.Thread(target=self.read_events, daemon=True).start()
        except OSError:
            self.process = None

    def read_events(self):
        collecting = False
        strings = []
        for line in self.process.stdout:
            if "member=Notify" in line:
                collecting = True
                strings = []
                continue
            if not collecting:
                continue
            text = line.strip()
            if not text.startswith("string "):
                continue
            try:
                strings.append(ast.literal_eval(text[7:]))
            except (SyntaxError, ValueError):
                continue
            if len(strings) == 4:
                title = strings[2].replace("\n", " ").strip()
                message = strings[3].replace("\n", " ").strip()
                if title:
                    with self.lock:
                        self.events.append((title, message))
                collecting = False

    def pop(self):
        with self.lock:
            return self.events.popleft() if self.events else None


def cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        preferred = ["coretemp", "k10temp", "cpu_thermal", "soc_thermal"]
        for key in preferred:
            if key in temps and temps[key]:
                return max(float(x.current) for x in temps[key])
        vals = [float(x.current) for group in temps.values() for x in group]
        if vals:
            return max(vals)
    except Exception:
        pass
    return 0.0


def gpu_stats():
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=0.35,
            stderr=subprocess.DEVNULL,
        ).splitlines()[0]
        util, temp, used, total = [float(x.strip()) for x in out.split(",")]
        mem_pct = (used / total * 100.0) if total > 0 else 0.0
        return util, temp, mem_pct
    except Exception:
        return None, None, None


def now_playing():
    try:
        output = subprocess.check_output(
            ["playerctl", "metadata", "--format", "{{artist}}\t{{title}}\t{{mpris:length}}"],
            text=True,
            timeout=0.35,
            stderr=subprocess.DEVNULL,
        ).rstrip("\n")
        artist, title, duration_us = output.split("\t", 2)
        position = float(subprocess.check_output(
            ["playerctl", "position"], text=True, timeout=0.35, stderr=subprocess.DEVNULL
        ).strip())
        status = subprocess.check_output(
            ["playerctl", "status"], text=True, timeout=0.35, stderr=subprocess.DEVNULL
        ).strip()
        duration = float(duration_us) / 1_000_000.0 if duration_us.strip() else 0.0
        return artist.strip(), title.strip(), position, duration, status
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="Pi hostname or IP, e.g. rmine.local")
    ap.add_argument("--port", type=int, default=7355)
    ap.add_argument("--interval", type=float, default=1.0 / 30.0)
    ap.add_argument("--audio-source", help="PipeWire/PulseAudio monitor source")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    prev_net = psutil.net_io_counters()
    prev_t = time.monotonic()
    psutil.cpu_percent(interval=None)

    gpu_cache = (None, None, None)
    last_gpu = 0.0
    track_cache = ("", "", 0.0, 0.0, "")
    last_track = 0.0
    last_track_success = 0.0
    track_key = None
    pause_key = None
    pause_started = 0.0
    notifications = NotificationMonitor()
    notice = None
    notice_id = 0
    notice_until = 0.0
    audio_last_active = 0.0
    audio_state = "standby"
    audio = AudioMonitor(args.audio_source)

    while True:
        now = time.monotonic()
        net = psutil.net_io_counters()
        dt = max(now - prev_t, 0.001)
        rx = max(0.0, (net.bytes_recv - prev_net.bytes_recv) / dt / 1_000_000)
        tx = max(0.0, (net.bytes_sent - prev_net.bytes_sent) / dt / 1_000_000)
        prev_net = net
        prev_t = now

        if now - last_gpu >= 1.0:
            gpu_cache = gpu_stats()
            last_gpu = now
        if now - last_track >= 1.0:
            last_track = now
            current_track = now_playing()
            if current_track is not None:
                track_cache = current_track
                last_track_success = now
                artist, title, _, _, status = track_cache
                new_track_key = (artist, title)
                if status == "Playing" and artist and title and track_key is not None and new_track_key != track_key:
                    notice_id += 1
                    notice = {"id": notice_id, "kind": "music", "title": "NOW PLAYING", "message": f"{artist} - {title}"}
                    notice_until = now + 4.0
                track_key = new_track_key
            elif now - last_track_success > 5.0:
                track_cache = ("", "", 0.0, 0.0, "")

        desktop_notice = notifications.pop()
        if desktop_notice:
            notice_id += 1
            title, message = desktop_notice
            notice = {"id": notice_id, "kind": "system", "title": title, "message": message}
            notice_until = now + 4.0
        if now >= notice_until:
            notice = None

        gu, gt, gm = gpu_cache
        artist, title, track_position, track_duration, track_status = track_cache
        if track_status == "Playing":
            audio_state = "playing"
        elif track_status == "Paused":
            audio_state = "paused"
        else:
            if audio.overall >= 12.0:
                audio_last_active = now
            audio_state = "playing" if now - audio_last_active < 3.0 else "standby"
        current_pause_key = (artist, title) if track_status == "Paused" else None
        if current_pause_key != pause_key:
            pause_key = current_pause_key
            pause_started = now
        paused_for = now - pause_started if pause_key else 0.0
        msg = {
            "hostname": socket.gethostname(),
            "cpu": float(psutil.cpu_percent(interval=None)),
            "mem": float(psutil.virtual_memory().percent),
            "cpu_temp": float(cpu_temp()),
            "rx": rx,
            "tx": tx,
            "uptime": max(0.0, time.time() - psutil.boot_time()),
            "gpu_util": gu,
            "gpu_temp": gt,
            "gpu_mem": gm,
            "audio_left": audio.levels[0],
            "audio_right": audio.levels[1],
            "audio_bands": audio.bands,
            "audio_bass": audio.bass,
            "audio_overall": audio.overall,
            "track_artist": artist,
            "track_title": title,
            "track_position": track_position,
            "track_duration": track_duration,
            "track_status": track_status,
            "track_paused_for": paused_for,
            "audio_state": audio_state,
            "notification": notice,
        }

        sock.sendto(json.dumps(msg, separators=(",", ":")).encode("utf-8"), (args.target, args.port))
        time.sleep(max(0.05, args.interval))


if __name__ == "__main__":
    main()
