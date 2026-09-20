#!/usr/bin/env python3
import argparse
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


SCENES = [
    ("status", "Core Status"), ("history", "Load History"), ("network", "Network"),
    ("clock", "Clock"), ("starfield", "Deep Field"), ("radar", "Radar"),
    ("wireframe", "Vector Object"), ("scope", "Oscilloscope"),
    ("life", "Cellular Field"), ("hex", "Memory Window"),
    ("diagnostic", "Diagnostic Scan"),
]

PAGE = """<!doctype html><meta charset=utf-8><title>Saladasz Soft Panel Control</title>
<style>body{{background:#10140f;color:#c9f8b4;font:16px monospace;margin:0}}main{{max-width:660px;margin:7vh auto;padding:26px;border:1px solid #406b39}}form{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}button{{background:#172017;border:1px solid #4d9b45;color:#d7ffd1;cursor:pointer;font:inherit;padding:13px}}.auto{{grid-column:1/-1;background:#315a2b}}</style>
<main><h1>SALADASZ SOFT // PANEL CONTROL</h1><p>Select a scene to hold it on the OLED. Auto Cycle resumes normal rotation.</p><form method=post><button class=auto name=scene value=auto>AUTO CYCLE</button>{buttons}</form></main>"""


class ControlHandler(BaseHTTPRequestHandler):
    target = None

    def do_GET(self):
        buttons = "".join(f'<button name=scene value={key}>{label}</button>' for key, label in SCENES)
        page = PAGE.format(buttons=buttons).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        scene = parse_qs(self.rfile.read(size).decode()).get("scene", [""])[0]
        if scene == "auto" or scene in dict(SCENES):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(json.dumps({"scene": scene}).encode(), (self.target, 7356))
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", help="Pi hostname or IP")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    ControlHandler.target = args.target
    ThreadingHTTPServer(("127.0.0.1", args.port), ControlHandler).serve_forever()


if __name__ == "__main__":
    main()
