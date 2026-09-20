#!/usr/bin/env python3
import argparse
import json
import socket
import subprocess
import time

import psutil


def cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "cpu_thermal", "soc_thermal"):
            if key in temps and temps[key]:
                return max(float(item.current) for item in temps[key])
        values = [float(item.current) for group in temps.values() for item in group]
        if values:
            return max(values)
    except Exception:
        pass
    return 0.0


def gpu_stats():
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=0.35,
            stderr=subprocess.DEVNULL,
        ).splitlines()[0]
        util, temp, used, total = [float(value.strip()) for value in output.split(",")]
        return util, temp, used / total * 100.0 if total else 0.0
    except Exception:
        return None, None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", help="Pi hostname or IP")
    parser.add_argument("--port", type=int, default=7355)
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    previous_net = psutil.net_io_counters()
    previous_time = time.monotonic()
    psutil.cpu_percent(interval=None)
    gpu_cache = (None, None, None)
    last_gpu = 0.0

    while True:
        now = time.monotonic()
        net = psutil.net_io_counters()
        elapsed = max(now - previous_time, 0.001)
        rx = max(0.0, (net.bytes_recv - previous_net.bytes_recv) / elapsed / 1_000_000)
        tx = max(0.0, (net.bytes_sent - previous_net.bytes_sent) / elapsed / 1_000_000)
        previous_net = net
        previous_time = now
        if now - last_gpu >= 1.0:
            gpu_cache = gpu_stats()
            last_gpu = now

        gpu_util, gpu_temp, gpu_mem = gpu_cache
        message = {
            "hostname": socket.gethostname(),
            "cpu": float(psutil.cpu_percent(interval=None)),
            "mem": float(psutil.virtual_memory().percent),
            "cpu_temp": cpu_temp(),
            "rx": rx,
            "tx": tx,
            "uptime": max(0.0, time.time() - psutil.boot_time()),
            "gpu_util": gpu_util,
            "gpu_temp": gpu_temp,
            "gpu_mem": gpu_mem,
        }
        sock.sendto(json.dumps(message, separators=(",", ":")).encode(), (args.target, args.port))
        time.sleep(max(0.05, args.interval))


if __name__ == "__main__":
    main()
