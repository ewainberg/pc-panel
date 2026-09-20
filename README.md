# Saladasz Soft Front Panel

Raspberry Pi OLED dashboard, Arduino Nano analog gauges, host telemetry sender, and a browser scene controller for a retro PC front panel.

## Components

- `pi/rmine_panel.py`: SSD1322 OLED scene system. Receives host telemetry on UDP `7355` and scene commands on UDP `7356`.
- `pi/oled_driver.c`: `liboled.so` driver for the Newhaven NHD-5.5-25664UCG3 using SPI0, CE0, GPIO25 D/C, and GPIO24 reset.
- `nano/rmine_gauges.ino`: MCP4728 gauge controller, rocker-triggered boot sweep, and D3 lamp PWM.
- `host/rmine_telemetry.py`: sends host CPU, RAM, temperature, network, and optional NVIDIA stats.
- `host/rmine_control.py`: local browser scene control at `http://127.0.0.1:8080`.

## Hardware

- OLED: SSD1322, 256x64, 4-wire SPI on Pi SPI0 CE0.
- Nano: CH340 USB serial at 115200 baud.
- MCP4728: I2C address `0x60`; physical gauge wiring uses logical-left on channel B and logical-right on channel A.
- Rocker sense: Nano D2.
- Incandescent lamp MOSFET PWM: Nano D3.

## Quick Start

On the Pi, create the Python environment and build the OLED library:

```bash
python3 -m venv ~/oled-env
~/oled-env/bin/pip install -r requirements.txt
gcc -shared -fPIC -O2 -o ~/liboled.so pi/oled_driver.c -llgpio
```

Compile/upload `nano/rmine_gauges.ino` as an Arduino Nano with the old ATmega328P bootloader. Upload at 57600 baud if needed.

From the host PC:

```bash
pip install -r requirements.txt
python host/rmine_telemetry.py PI_IP
python host/rmine_control.py PI_IP
```

Install the service templates after replacing usernames and paths for your installation.
