#!/usr/bin/env python3
import ctypes
import glob
import json
import math
import os
import random
import signal
import socket
import subprocess
import time
from collections import deque
from datetime import datetime

import psutil
from PIL import Image, ImageDraw, ImageFont

try:
    import serial
except ImportError:
    serial = None

WIDTH = 256
HEIGHT = 64
FPS = 10.0
FRAME_INTERVAL = 1.0 / FPS
TRANSITION_FPS = 18.0
TRANSITION_FRAMES = 8
TELEMETRY_PORT = 7355
TELEMETRY_STALE = 2.5
CONTROL_PORT = 7356
OLED_LIB = "/home/maxpower/liboled.so"


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def norm(v, lo, hi):
    if hi <= lo:
        return 0.0
    return clamp((v - lo) * 100.0 / (hi - lo))


def font_path(pattern, fallbacks):
    try:
        out = subprocess.check_output(
            ["fc-match", "-f", "%{file}\n", pattern],
            text=True,
            stderr=subprocess.DEVNULL,
        ).splitlines()
        for p in out:
            if os.path.exists(p):
                return p
    except Exception:
        pass
    for p in fallbacks:
        if os.path.exists(p):
            return p
    return None


REGULAR_PATH = font_path(
    "DejaVu Sans Mono",
    [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
    ],
)
BOLD_PATH = font_path(
    "DejaVu Sans Mono:style=Bold",
    [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    ],
)

if not REGULAR_PATH or not BOLD_PATH:
    raise RuntimeError(
        "No usable TrueType monospace font found. Install one with: "
        "sudo apt install fonts-dejavu-core"
    )

# V2: small text is a little larger than v1. More importantly, every draw
# context below uses fontmode='1', which keeps tiny text hard-edged/pixel crisp
# instead of smearing it with grayscale antialiasing.
F_TINY = ImageFont.truetype(REGULAR_PATH, 8)
F_SMALL = ImageFont.truetype(REGULAR_PATH, 9)
F_SMALL_B = ImageFont.truetype(BOLD_PATH, 9)
F_MED = ImageFont.truetype(REGULAR_PATH, 10)
F_MED_B = ImageFont.truetype(BOLD_PATH, 10)
F_BIG = ImageFont.truetype(BOLD_PATH, 15)
F_HUGE = ImageFont.truetype(BOLD_PATH, 28)


def canvas():
    return Image.new("L", (WIDTH, HEIGHT), 0)


def make_draw(image):
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"  # no antialiasing on text; grayscale graphics still work
    return draw


class OLED:
    def __init__(self, lib_path=OLED_LIB):
        self.lib = ctypes.CDLL(lib_path)
        self.lib.oled_init.restype = ctypes.c_int
        self.lib.oled_send_frame.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
        self.lib.oled_close.argtypes = []
        self.lib.oled_close.restype = None
        rc = self.lib.oled_init()
        if rc != 0:
            raise RuntimeError(f"OLED initialization failed: {rc}")

    @staticmethod
    def image_to_frame(image):
        if image.mode != "L":
            image = image.convert("L")
        px = image.load()
        fb = bytearray(8192)
        i = 0
        for y in range(HEIGHT):
            for x in range(0, WIDTH, 2):
                p1 = px[x, y] >> 4
                p2 = px[x + 1, y] >> 4
                fb[i] = (p1 << 4) | p2
                i += 1
        return fb

    def send(self, image):
        data = self.image_to_frame(image)
        ctype = ctypes.c_uint8 * len(data)
        buf = ctype.from_buffer_copy(data)
        self.lib.oled_send_frame(buf)

    def clear(self):
        self.send(canvas())

    def close(self):
        self.lib.oled_close()


class GaugeLink:
    def __init__(self, baud=115200):
        self.ser = None
        self.port = None
        self.last = None
        if serial is None:
            return

        candidates = sorted(glob.glob("/dev/serial/by-id/*"))
        candidates += ["/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyUSB1", "/dev/ttyACM1"]
        seen = set()
        for port in candidates:
            if port in seen or not os.path.exists(port):
                continue
            seen.add(port)
            try:
                self.ser = serial.Serial(port, baud, timeout=0, write_timeout=0.2)
                self.port = port
                time.sleep(1.6)  # Nano/CH340 may reset when serial opens
                self.ser.reset_input_buffer()
                break
            except Exception:
                self.ser = None
                self.port = None

    @property
    def connected(self):
        return self.ser is not None

    def send(self, left, right, force=False):
        left = int(round(clamp(left)))
        right = int(round(clamp(right)))
        pair = (left, right)
        if not force and pair == self.last:
            return
        self.last = pair
        if not self.ser:
            return
        try:
            self.ser.write(f"{left},{right}\n".encode("ascii"))
        except Exception:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def close(self):
        if self.ser:
            try:
                self.send(0, 0, force=True)
                time.sleep(0.08)
                self.ser.close()
            except Exception:
                pass
        self.ser = None


class LocalTelemetry:
    def __init__(self):
        self.prev_net = psutil.net_io_counters()
        self.prev_t = time.monotonic()
        psutil.cpu_percent(interval=None)

    @staticmethod
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
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return int(f.read().strip()) / 1000.0
        except Exception:
            return 0.0

    def sample(self):
        now = time.monotonic()
        net = psutil.net_io_counters()
        dt = max(now - self.prev_t, 0.001)
        rx = max(0.0, (net.bytes_recv - self.prev_net.bytes_recv) / dt / 1_000_000)
        tx = max(0.0, (net.bytes_sent - self.prev_net.bytes_sent) / dt / 1_000_000)
        self.prev_net = net
        self.prev_t = now
        return {
            "source": "LOCAL",
            "hostname": socket.gethostname(),
            "cpu": float(psutil.cpu_percent(interval=None)),
            "mem": float(psutil.virtual_memory().percent),
            "cpu_temp": float(self.cpu_temp()),
            "rx": rx,
            "tx": tx,
            "uptime": max(0.0, time.time() - psutil.boot_time()),
            "gpu_util": None,
            "gpu_temp": None,
            "gpu_mem": None,
        }


class TelemetryReceiver:
    def __init__(self, port=TELEMETRY_PORT):
        self.local = LocalTelemetry()
        self.latest = None
        self.latest_at = 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.setblocking(False)

    def poll(self):
        while True:
            try:
                raw, _ = self.sock.recvfrom(8192)
            except BlockingIOError:
                break
            except Exception:
                break
            try:
                msg = json.loads(raw.decode("utf-8"))
                msg["source"] = "HOST"
                self.latest = msg
                self.latest_at = time.monotonic()
            except Exception:
                pass

        if self.latest and time.monotonic() - self.latest_at <= TELEMETRY_STALE:
            return dict(self.latest)
        return self.local.sample()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class ControlReceiver:
    def __init__(self, port=CONTROL_PORT):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.setblocking(False)

    def poll(self):
        scene = None
        while True:
            try:
                raw, _ = self.sock.recvfrom(1024)
            except BlockingIOError:
                break
            try:
                command = json.loads(raw.decode("utf-8"))
                if isinstance(command.get("scene"), str):
                    scene = command["scene"]
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        return scene

    def close(self):
        self.sock.close()


class History:
    def __init__(self, length=300):
        self.cpu = deque(maxlen=length)
        self.mem = deque(maxlen=length)
        self.rx = deque(maxlen=length)
        self.tx = deque(maxlen=length)
        self.cpu_temp = deque(maxlen=length)
        self.gpu = deque(maxlen=length)
        self.gpu_temp = deque(maxlen=length)
        self.rx_peak = 0.2
        self.tx_peak = 0.2

    def push(self, m):
        self.cpu.append(float(m.get("cpu") or 0))
        self.mem.append(float(m.get("mem") or 0))
        self.rx.append(float(m.get("rx") or 0))
        self.tx.append(float(m.get("tx") or 0))
        self.cpu_temp.append(float(m.get("cpu_temp") or 0))
        gu = m.get("gpu_util")
        gt = m.get("gpu_temp")
        self.gpu.append(float(gu) if gu is not None else 0.0)
        self.gpu_temp.append(float(gt) if gt is not None else 0.0)
        self.rx_peak = max(float(m.get("rx") or 0), self.rx_peak * 0.992, 0.2)
        self.tx_peak = max(float(m.get("tx") or 0), self.tx_peak * 0.992, 0.2)


# -----------------------------------------------------------------------------
# Persistent ambient state
# -----------------------------------------------------------------------------

random.seed(1337)
STARFIELD = [
    (
        random.uniform(-1.6, 1.6),
        random.uniform(-0.75, 0.75),
        random.uniform(0.10, 1.00),
        random.uniform(0.10, 0.22),
    )
    for _ in range(90)
]
RADAR_BLIPS = [
    (random.uniform(0, math.tau), random.uniform(0.25, 0.92), random.uniform(0.55, 1.0))
    for _ in range(9)
]


class LifeField:
    def __init__(self, cols=64, rows=13):
        self.cols = cols
        self.rows = rows
        self.rng = random.Random(424242)
        self.grid = []
        self.glow = []
        self.generation = 0
        self.last_step = -999.0
        self.reset()

    def reset(self):
        self.grid = [
            [1 if self.rng.random() < 0.27 else 0 for _ in range(self.cols)]
            for _ in range(self.rows)
        ]
        self.glow = [[255 if c else 0 for c in row] for row in self.grid]
        self.generation = 0

    def step(self):
        nxt = [[0] * self.cols for _ in range(self.rows)]
        for y in range(self.rows):
            for x in range(self.cols):
                n = 0
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        n += self.grid[(y + dy) % self.rows][(x + dx) % self.cols]
                alive = self.grid[y][x]
                nxt[y][x] = 1 if (n == 3 or (alive and n == 2)) else 0

        # A stable field has no future movement, so start a new simulation.
        if nxt == self.grid:
            self.reset()
            return

        for y in range(self.rows):
            for x in range(self.cols):
                if nxt[y][x]:
                    self.glow[y][x] = 245
                else:
                    self.glow[y][x] = int(self.glow[y][x] * 0.55)

        self.grid = nxt
        self.generation += 1
        pop = sum(sum(row) for row in self.grid)
        total = self.cols * self.rows
        if pop < max(8, total // 50) or pop > int(total * 0.72):
            self.reset()

    def update(self, scene_t):
        # scene_t resets on every scene entry. Detect the reset and allow a step
        # immediately so the field never gets stuck after revisiting the scene.
        if scene_t < self.last_step:
            self.last_step = -999.0
        if scene_t - self.last_step >= 0.16:
            self.step()
            self.last_step = scene_t


LIFE = LifeField()


# -----------------------------------------------------------------------------
# Drawing helpers
# -----------------------------------------------------------------------------


def text_width(draw, text, font):
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0]


def right_text(draw, x, y, text, font=F_SMALL, fill=255):
    draw.text((x - text_width(draw, text, font), y), text, font=font, fill=fill)


def center_text(draw, y, text, font, fill=255):
    w = text_width(draw, text, font)
    draw.text(((WIDTH - w) // 2, y), text, font=font, fill=fill)


def header(draw, title, m, clock=True):
    draw.text((4, 1), title, font=F_SMALL_B, fill=220)
    src = m.get("hostname", m.get("source", "LOCAL"))
    if clock:
        right_text(draw, 252, 1, datetime.now().strftime("%H:%M"), F_SMALL_B, 235)
        right_text(draw, 207, 1, src, F_TINY, 65)
    else:
        right_text(draw, 252, 1, src, F_TINY, 65)
    draw.line((4, 11, 252, 11), fill=28)


def ambient_header(draw, left, right="AMBIENT"):
    draw.text((4, 1), left, font=F_SMALL_B, fill=155)
    right_text(draw, 252, 1, right, F_TINY, 55)


def gauge_legend(draw, left, right):
    draw.line((4, 53, 252, 53), fill=18)
    draw.text((4, 55), f"L {left}", font=F_TINY, fill=130)
    right_text(draw, 252, 55, f"{right} R", F_TINY, 130)


def graph_line(draw, values, x, y, w, h, max_value=100.0, fill=220, min_value=0.0):
    vals = list(values)
    if len(vals) < 2:
        return
    vals = vals[-w:]
    n = len(vals)
    pts = []
    span = max(max_value - min_value, 1e-6)
    for i, v in enumerate(vals):
        px = x + int(i * (w - 1) / max(n - 1, 1))
        frac = clamp((float(v) - min_value) / span, 0.0, 1.0)
        py = y + h - 1 - int(frac * (h - 1))
        pts.append((px, py))
    if len(pts) >= 2:
        draw.line(pts, fill=fill, width=1)


def fmt_rate(mbps):
    mbps = max(0.0, float(mbps))
    if mbps >= 100:
        return f"{mbps:4.0f}M"
    if mbps >= 1:
        return f"{mbps:4.1f}M"
    kbps = mbps * 1000.0
    if kbps >= 1:
        return f"{kbps:4.0f}K"
    return "   0K"


def fmt_uptime(seconds):
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h:02}h {m:02}m"
    return f"{h:02}h {m:02}m"


def deterministic_byte(seed):
    x = seed & 0xFFFFFFFF
    x ^= (x << 13) & 0xFFFFFFFF
    x ^= (x >> 17) & 0xFFFFFFFF
    x ^= (x << 5) & 0xFFFFFFFF
    return x & 0xFF


# -----------------------------------------------------------------------------
# Useful scenes
# -----------------------------------------------------------------------------


def draw_status(m, h, t):
    im = canvas()
    d = make_draw(im)
    header(d, "SALADASZ SOFT // CORE", m)

    d.text((4, 14), "CPU", font=F_TINY, fill=80)
    d.text((4, 21), f"{m['cpu']:02.0f}%", font=F_BIG, fill=255)

    d.text((92, 14), "MEM", font=F_TINY, fill=80)
    d.text((92, 21), f"{m['mem']:02.0f}%", font=F_BIG, fill=220)

    d.text((181, 14), "TEMP", font=F_TINY, fill=80)
    d.text((181, 21), f"{m['cpu_temp']:02.0f}C", font=F_BIG, fill=255)

    d.line((4, 39, 252, 39), fill=18)
    graph_line(d, h.cpu, 4, 40, 248, 11, 100, 180)

    gauge_legend(d, f"CPU {m['cpu']:.0f}%", f"TEMP {m['cpu_temp']:.0f}C")
    return im, m["cpu"], norm(m["cpu_temp"], 35, 85)


def draw_history(m, h, t):
    im = canvas()
    d = make_draw(im)
    header(d, "LOAD HISTORY", m)

    gx, gy, gw, gh = 4, 14, 248, 36
    for pct in (25, 50, 75):
        yy = gy + gh - int(gh * pct / 100)
        d.line((gx, yy, gx + gw, yy), fill=15)
    graph_line(d, h.mem, gx, gy, gw, gh, 100, 70)
    graph_line(d, h.cpu, gx, gy, gw, gh, 100, 235)
    d.text((6, 15), "CPU", font=F_TINY, fill=230)
    d.text((34, 15), "MEM", font=F_TINY, fill=75)
    gauge_legend(d, f"CPU {m['cpu']:.0f}%", f"MEM {m['mem']:.0f}%")
    return im, m["cpu"], m["mem"]


def draw_network(m, h, t):
    im = canvas()
    d = make_draw(im)
    header(d, "NETWORK", m)

    d.text((4, 14), "RX", font=F_TINY, fill=80)
    d.text((4, 21), fmt_rate(m["rx"]), font=F_BIG, fill=255)
    d.text((72, 27), "/s", font=F_TINY, fill=70)

    d.text((137, 14), "TX", font=F_TINY, fill=80)
    d.text((137, 21), fmt_rate(m["tx"]), font=F_BIG, fill=210)
    d.text((205, 27), "/s", font=F_TINY, fill=70)

    scale = max(max(h.rx, default=0), max(h.tx, default=0), 0.2)
    graph_line(d, h.tx, 4, 39, 248, 12, scale, 65)
    graph_line(d, h.rx, 4, 39, 248, 12, scale, 225)

    left = clamp((m["rx"] / max(h.rx_peak, 0.001)) * 100.0)
    right = clamp((m["tx"] / max(h.tx_peak, 0.001)) * 100.0)
    gauge_legend(d, f"RX {fmt_rate(m['rx']).strip()}", f"TX {fmt_rate(m['tx']).strip()}")
    return im, left, right


def draw_gpu(m, h, t):
    im = canvas()
    d = make_draw(im)
    header(d, "GPU", m)
    util = float(m.get("gpu_util") or 0)
    temp = float(m.get("gpu_temp") or 0)
    vram = m.get("gpu_mem")

    d.text((4, 14), "LOAD", font=F_TINY, fill=80)
    d.text((4, 21), f"{util:02.0f}%", font=F_BIG, fill=255)

    d.text((90, 14), "TEMP", font=F_TINY, fill=80)
    d.text((90, 21), f"{temp:02.0f}C", font=F_BIG, fill=220)

    d.text((177, 14), "VRAM", font=F_TINY, fill=80)
    vram_text = "--" if vram is None else f"{float(vram):.0f}%"
    d.text((177, 21), vram_text, font=F_BIG, fill=200)

    graph_line(d, h.gpu, 4, 40, 248, 11, 100, 220)
    gauge_legend(d, f"GPU {util:.0f}%", f"TEMP {temp:.0f}C")
    return im, util, norm(temp, 35, 90)


def draw_clock(m, h, t):
    im = canvas()
    d = make_draw(im)
    now = datetime.now()
    center_text(d, -2, now.strftime("%H:%M"), F_HUGE, 255)
    center_text(d, 31, now.strftime("%a %d %b %Y").upper(), F_SMALL_B, 125)
    center_text(d, 42, f"UP {fmt_uptime(m.get('uptime', 0))}", F_SMALL, 70)
    gauge_legend(d, f"CPU {m['cpu']:.0f}%", f"TEMP {m['cpu_temp']:.0f}C")
    return im, m["cpu"], norm(m["cpu_temp"], 35, 85)


# -----------------------------------------------------------------------------
# Ambient scenes
# -----------------------------------------------------------------------------


def draw_starfield(m, h, t):
    im = canvas()
    d = make_draw(im)
    ambient_header(d, "DEEP FIELD", "NAV // 04")
    cx, cy = 128, 31

    for sx, sy, z0, speed in STARFIELD:
        z = ((z0 - t * speed) % 0.95) + 0.05
        scale = 34.0 / z
        x = int(cx + sx * scale)
        y = int(cy + sy * scale)
        if not (1 <= x < WIDTH - 1 and 12 <= y <= 51):
            continue
        z_prev = min(1.0, z + 0.045)
        scale_prev = 34.0 / z_prev
        px = int(cx + sx * scale_prev)
        py = int(cy + sy * scale_prev)
        bright = int(clamp((1.05 - z) * 255.0, 35, 255))
        d.line((px, py, x, y), fill=max(22, bright // 2))
        d.point((x, y), fill=bright)

    # tiny reticle gives it an instrument-screen feel without boxing everything
    d.line((124, 31, 132, 31), fill=30)
    d.line((128, 27, 128, 35), fill=30)
    left = 50 + 35 * math.sin(t * 0.55)
    right = 50 + 35 * math.sin(t * 0.55 + 1.9)
    gauge_legend(d, "DRIFT", "PARALLAX")
    return im, left, right


def draw_radar(m, h, t):
    im = canvas()
    d = make_draw(im)
    ambient_header(d, "RADAR // PASSIVE", "RNG 12")
    cx, cy, r = 128, 32, 18

    for rr, fill in ((r, 70), (12, 28), (6, 18)):
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=fill)
    d.line((cx - r, cy, cx + r, cy), fill=18)
    d.line((cx, cy - r, cx, cy + r), fill=18)

    angle = (t * 1.55) % math.tau
    # fading phosphor trail
    for i in range(8, -1, -1):
        a = angle - i * 0.055
        ex = cx + int(math.cos(a) * r)
        ey = cy + int(math.sin(a) * r)
        d.line((cx, cy, ex, ey), fill=25 + (8 - i) * 20)

    strongest = 0.0
    contacts = 0
    for a, radius, strength in RADAR_BLIPS:
        bx = cx + int(math.cos(a) * r * radius)
        by = cy + int(math.sin(a) * r * radius)
        age = (angle - a) % math.tau
        if age < 1.15:
            glow = int(255 * strength * (1.0 - age / 1.15))
            if glow > 20:
                d.point((bx, by), fill=glow)
                if glow > 120:
                    d.rectangle((bx - 1, by - 1, bx + 1, by + 1), outline=glow)
                    contacts += 1
                strongest = max(strongest, glow / 255.0)

    d.text((4, 18), f"AZ {math.degrees(angle):03.0f}", font=F_TINY, fill=85)
    d.text((4, 29), f"CT {contacts:02d}", font=F_TINY, fill=85)
    right_text(d, 252, 18, "SWEEP", F_TINY, 65)
    right_text(d, 252, 29, f"SIG {int(strongest * 99):02d}", F_TINY, 100)

    gauge_legend(d, "AZIMUTH", "CONTACT")
    return im, (angle / math.tau) * 100.0, strongest * 100.0


def draw_scope(m, h, t):
    im = canvas()
    d = make_draw(im)
    ambient_header(d, "OSC // NOISE", "CH A+B")
    mid = 31
    amp = 5.0 + 6.0 * (m["cpu"] / 100.0)

    p1, p2, p3 = [], [], []
    for x in range(WIDTH):
        phase = x * 0.083
        # deterministic, continuous pseudo-noise instead of per-frame random flicker
        noise = math.sin(x * 2.173 + t * 11.0) * math.sin(x * 0.319 - t * 7.7)
        y1 = mid + math.sin(phase + t * 3.1) * amp + noise * 1.8
        y2 = mid + math.sin(phase * 0.49 - t * 1.6) * (amp * 0.65)
        y3 = mid + math.sin(phase * 1.77 + t * 4.2) * 1.8
        p1.append((x, int(y1)))
        p2.append((x, int(y2)))
        p3.append((x, int(y3)))

    d.line((0, mid, 255, mid), fill=16)
    for x in range(0, WIDTH, 32):
        d.line((x, 14, x, 50), fill=10)
    d.line(p2, fill=42, width=1)
    d.line(p3, fill=85, width=1)
    d.line(p1, fill=230, width=1)

    left = 50 + 45 * math.sin(t * 0.8)
    right = 50 + 45 * math.sin(t * 1.15 + 1.2)
    gauge_legend(d, "CH-A", "CH-B")
    return im, left, right


CUBE_VERTICES = [
    (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
    (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
]
CUBE_EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
]


def rotate_point(p, ax, ay, az):
    x, y, z = p
    cy, sy = math.cos(ay), math.sin(ay)
    x, z = x * cy + z * sy, -x * sy + z * cy
    cx, sx = math.cos(ax), math.sin(ax)
    y, z = y * cx - z * sx, y * sx + z * cx
    cz, sz = math.cos(az), math.sin(az)
    x, y = x * cz - y * sz, x * sz + y * cz
    return x, y, z


def draw_wireframe(m, h, t):
    im = canvas()
    d = make_draw(im)
    ambient_header(d, "VECTOR OBJECT", "MESH // 8V")

    ax, ay, az = t * 0.73, t * 0.92, t * 0.37
    cx = 128 + int(math.sin(t * 0.62) * 62)
    cy = 32 + int(math.sin(t * 0.91) * 5)
    projected = []
    depth = []
    for p in CUBE_VERTICES:
        x, y, z = rotate_point(p, ax, ay, az)
        zz = z + 4.2
        sc = 25.0 / zz
        projected.append((int(cx + x * sc), int(cy + y * sc)))
        depth.append(zz)

    for a, b in CUBE_EDGES:
        zavg = (depth[a] + depth[b]) / 2.0
        brightness = int(clamp(250 - (zavg - 3.0) * 55, 55, 235))
        d.line((*projected[a], *projected[b]), fill=brightness)

    # moving floor marker
    d.line((cx - 14, 49, cx + 14, 49), fill=20)
    d.line((cx, 46, cx, 51), fill=20)
    gauge_legend(d, "YAW", "ROLL")
    return im, ((ay % math.tau) / math.tau) * 100, ((az % math.tau) / math.tau) * 100


def draw_life(m, h, t):
    im = canvas()
    d = make_draw(im)
    LIFE.update(t)
    ambient_header(d, "GAME OF LIFE", f"GEN {LIFE.generation:04d}")

    cell_w = 4
    cell_h = 3
    y0 = 13
    for y in range(LIFE.rows):
        for x in range(LIFE.cols):
            v = LIFE.glow[y][x]
            if v < 8:
                continue
            x0 = x * cell_w
            yy = y0 + y * cell_h
            d.rectangle((x0, yy, x0 + 2, yy + 1), fill=v)

    population = sum(sum(row) for row in LIFE.grid)
    pct = population * 100.0 / (LIFE.cols * LIFE.rows)
    gauge_legend(d, f"POP {pct:02.0f}%", "GEN RATE")
    return im, clamp(pct * 3.2), 50 + 45 * math.sin(t * 0.9)


def draw_hex(m, h, t):
    im = canvas()
    d = make_draw(im)
    ambient_header(d, "MEMORY WINDOW", "HEX // LIVE")

    tick = int(t * 9)
    base = 0x7F00 + (tick // 2) * 0x10
    for row in range(5):
        addr = (base + row * 0x10) & 0xFFFF
        values = [deterministic_byte(addr * 131 + col * 977 + tick * 17) for col in range(8)]
        line = f"{addr:04X}  " + " ".join(f"{v:02X}" for v in values)
        y = 13 + row * 8
        d.text((4, y), line, font=F_TINY, fill=95 if row != 2 else 225)

        # scanner block crawling through the bytes
        hot_col = (tick + row * 3) % 8
        before = f"{addr:04X}  " + " ".join(f"{v:02X}" for v in values[:hot_col])
        if hot_col:
            before += " "
        hx = 4 + d.textlength(before, font=F_TINY)
        bounds = d.textbbox((hx, y), f"{values[hot_col]:02X}", font=F_TINY)
        d.rectangle(
            (bounds[0] - 1, bounds[1] - 1, bounds[2], bounds[3]),
            outline=35 if row != 2 else 95,
        )

    gauge_legend(d, "ADDR", "BUS")
    return im, (tick * 7) % 100, (tick * 13 + 35) % 100


DIAG_ITEMS = [
    "MEMORY BUS",
    "I/O FABRIC",
    "THERMAL ARRAY",
    "NET LINK",
    "GPU CHANNEL",
]


def draw_diagnostic(m, h, t):
    im = canvas()
    d = make_draw(im)
    ambient_header(d, "DIAGNOSTIC SCAN", "SERVICE // SIM")

    stage = min(int(t / 0.85), len(DIAG_ITEMS))
    for i, item in enumerate(DIAG_ITEMS):
        y = 13 + i * 8
        d.text((4, y), item, font=F_TINY, fill=95)
        if i < stage:
            right_text(d, 250, y, "PASS", F_TINY, 220)
        elif i == stage:
            pulse = int(105 + 110 * (0.5 + 0.5 * math.sin(t * 12)))
            right_text(d, 250, y, "SCAN", F_TINY, pulse)
            # short animated scan bar between name and status
            x2 = 132 + int((t * 75) % 72)
            d.line((132, y + 6, min(204, x2), y + 6), fill=80)
        else:
            right_text(d, 250, y, "----", F_TINY, 38)

    if stage >= len(DIAG_ITEMS):
        # Keep the completion bar clear of the final status row and legend.
        d.rectangle((116, 54, 250, 55), fill=45)
        d.line((116, 54, 250, 54), fill=120)

    positions = [8, 25, 50, 75, 100, 0]
    pos = positions[min(stage, len(positions) - 1)]
    gauge_legend(d, "TEST A", "TEST B")
    return im, pos, 100 - pos


# -----------------------------------------------------------------------------
# Scene table
# -----------------------------------------------------------------------------

# The order deliberately alternates useful information and theatrical/ambient
# displays, so it never feels like a boring monitoring dashboard for long.
SCENES = [
    ("status", 8.0, draw_status),
    ("starfield", 7.0, draw_starfield),
    ("history", 8.0, draw_history),
    ("radar", 7.0, draw_radar),
    ("network", 8.0, draw_network),
    ("wireframe", 7.0, draw_wireframe),
    ("gpu", 8.0, draw_gpu),
    ("scope", 7.0, draw_scope),
    ("clock", 7.0, draw_clock),
    ("life", 7.0, draw_life),
    ("hex", 7.0, draw_hex),
    ("diagnostic", 7.0, draw_diagnostic),
]


def scene_available(name, m):
    if name == "gpu":
        return m.get("gpu_util") is not None
    return True


# -----------------------------------------------------------------------------
# Boot + transitions
# -----------------------------------------------------------------------------


def boot_sequence(oled, gauges):
    steps = [
        ("SALADASZ SOFT BUS", "OLED LINK", "ONLINE"),
        ("SALADASZ SOFT BUS", "SPI0 / SSD1322", "READY"),
        ("SALADASZ SOFT BUS", "GAUGE BUS", "ONLINE" if gauges.connected else "STANDBY"),
        ("SALADASZ SOFT BUS", "ANALOG CAL", "SWEEP"),
    ]
    for title, item, status in steps:
        im = canvas()
        d = make_draw(im)
        d.text((5, 3), title, font=F_SMALL_B, fill=220)
        d.line((5, 14, 251, 14), fill=30)
        d.text((5, 25), item, font=F_MED_B, fill=165)
        right_text(d, 251, 25, status, F_MED_B, 255)
        oled.send(im)
        time.sleep(0.38)

    if gauges.connected:
        gauges.send(100, 100, force=True)
        time.sleep(1.15)
        gauges.send(0, 0, force=True)
        time.sleep(1.15)

    im = canvas()
    d = make_draw(im)
    center_text(d, 17, "SYSTEM READY", F_BIG, 255)
    center_text(d, 38, "SALADASZ SOFT // FRONT PANEL V2", F_SMALL, 85)
    oled.send(im)
    time.sleep(0.55)


def scan_wipe(oled, old_im, new_im, frames=TRANSITION_FRAMES):
    """Fast hardware-paced scene wipe with a bright scan edge.

    V1 rendered at only 4 FPS and also slept after every wipe frame. V2 uses a
    faster SPI clock plus a short 18 FPS transition, which is much less choppy.
    """
    target_dt = 1.0 / TRANSITION_FPS
    for i in range(1, frames + 1):
        started = time.monotonic()
        x = int(WIDTH * i / frames)
        frame = old_im.copy()
        frame.paste(new_im.crop((0, 0, x, HEIGHT)), (0, 0))
        d = make_draw(frame)
        if 1 <= x < WIDTH - 1:
            d.line((x, 0, x, HEIGHT - 1), fill=240)
            if x > 1:
                d.line((x - 1, 0, x - 1, HEIGHT - 1), fill=70)
        oled.send(frame)
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, target_dt - elapsed))


running = True


def stop_program(signum=None, frame=None):
    global running
    running = False


signal.signal(signal.SIGINT, stop_program)
signal.signal(signal.SIGTERM, stop_program)


def main():
    oled = None
    gauges = None
    telemetry = None
    controls = None
    try:
        oled = OLED()
        gauges = GaugeLink()
        telemetry = TelemetryReceiver()
        controls = ControlReceiver()
        history = History()

        boot_sequence(oled, gauges)

        m = telemetry.poll()
        history.push(m)
        scene_index = 0
        while not scene_available(SCENES[scene_index][0], m):
            scene_index = (scene_index + 1) % len(SCENES)

        scene_started = time.monotonic()
        last_image = canvas()
        manual_scene = None

        while running:
            loop_start = time.monotonic()
            m = telemetry.poll()
            history.push(m)

            requested_scene = controls.poll()
            if requested_scene == "auto":
                manual_scene = None
            elif requested_scene:
                for index, (candidate, _, renderer) in enumerate(SCENES):
                    if candidate == requested_scene and scene_available(candidate, m):
                        manual_scene = candidate
                        scene_index = index
                        scene_started = loop_start
                        next_image, gl, gr = renderer(m, history, 0.0)
                        gauges.send(gl, gr)
                        scan_wipe(oled, last_image, next_image)
                        last_image = next_image
                        break
                continue

            name, duration, renderer = SCENES[scene_index]
            scene_elapsed = loop_start - scene_started

            if not scene_available(name, m) or (
                manual_scene is None and scene_elapsed >= duration
            ):
                if not scene_available(name, m):
                    manual_scene = None
                next_index = (scene_index + 1) % len(SCENES)
                for _ in range(len(SCENES)):
                    if scene_available(SCENES[next_index][0], m):
                        break
                    next_index = (next_index + 1) % len(SCENES)

                scene_index = next_index
                scene_started = time.monotonic()
                name, duration, renderer = SCENES[scene_index]
                next_image, gl, gr = renderer(m, history, 0.0)
                gauges.send(gl, gr)
                scan_wipe(oled, last_image, next_image)
                last_image = next_image
                continue

            image, gl, gr = renderer(m, history, scene_elapsed)
            gauges.send(gl, gr)
            oled.send(image)
            last_image = image

            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, FRAME_INTERVAL - elapsed))

    finally:
        if gauges:
            gauges.close()
        if oled:
            try:
                oled.clear()
                time.sleep(0.05)
            except Exception:
                pass
            try:
                oled.close()
            except Exception:
                pass
        if telemetry:
            telemetry.close()
        if controls:
            controls.close()
        print("OLED cleared; gauge targets zeroed.")


if __name__ == "__main__":
    main()
