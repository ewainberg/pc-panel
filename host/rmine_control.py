#!/usr/bin/env python3
import argparse
import curses
import json
import socket


SCENES = [
    ("status", "Core Status"),
    ("gpu", "GPU Monitor"),
    ("history", "Load History"),
    ("network", "Network"),
    ("clock", "Clock"),
    ("starfield", "Deep Field"),
    ("radar", "Radar"),
    ("wireframe", "Vector Object"),
    ("scope", "Oscilloscope"),
    ("audio", "Sound Reactive"),
    ("life", "Cellular Field"),
    ("hex", "Memory Window"),
    ("diagnostic", "Diagnostic Scan"),
]


def send_command(target, command):
    message = json.dumps(command).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(message, (target, 7356))


def run(screen, target):
    items = [("auto", "AUTO CYCLE")] + SCENES
    selected = 0
    lamp = 25
    status = "Arrows/J/K select. Enter sends. L cycles lamp brightness. Q quits."
    curses.curs_set(0)
    screen.keypad(True)

    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        screen.attron(curses.A_BOLD)
        screen.addnstr(1, 2, "SALADASZ SOFT // PANEL CONTROL", width - 4)
        screen.attroff(curses.A_BOLD)
        screen.addnstr(2, 2, f"TARGET {target}  |  LAMP {lamp:03d}%", width - 4)
        screen.hline(3, 2, curses.ACS_HLINE, max(0, width - 4))

        for index, (_, label) in enumerate(items):
            y = index + 5
            if y >= height - 2:
                break
            marker = ">" if index == selected else " "
            mode = curses.A_REVERSE if index == selected else curses.A_NORMAL
            screen.attron(mode)
            screen.addnstr(y, 3, f"{marker} {index:02d}  {label}", width - 6)
            screen.attroff(mode)

        screen.hline(height - 2, 2, curses.ACS_HLINE, max(0, width - 4))
        screen.addnstr(height - 1, 2, status, width - 4)
        screen.refresh()

        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (curses.KEY_UP, ord("k"), ord("K")):
            selected = (selected - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
            selected = (selected + 1) % len(items)
        elif key in (curses.KEY_ENTER, 10, 13):
            scene, label = items[selected]
            send_command(target, {"scene": scene})
            status = f"SENT: {label}"
        elif key in (ord("l"), ord("L")):
            lamp = (lamp + 25) % 125
            send_command(target, {"lamp": lamp})
            status = f"SENT: LAMP {lamp}%"
        elif ord("0") <= key <= ord("9"):
            index = key - ord("0")
            if index < len(items):
                selected = index


def main():
    parser = argparse.ArgumentParser(description="Panel TUI")
    parser.add_argument("target", nargs="?", default="10.6.25.9", help="Pi hostname or IP")
    args = parser.parse_args()
    curses.wrapper(run, args.target)


if __name__ == "__main__":
    main()
