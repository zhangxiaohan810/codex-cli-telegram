#!/usr/bin/env python3
"""Blink a Logitech G610 keyboard backlight at 10 Hz.

Requires the Python `hidapi` package (`python -m pip install hidapi`).
On macOS this usually needs sudo because the keyboard HID interface is
protected by the system.
"""

from __future__ import annotations

import signal
import sys
import time

import hid


VID = 0x046D
PID = 0xC338
USAGE_PAGE = 0xFF43
USAGE = 0x0604
HALF_PERIOD_SECONDS = 0.05

ON = [
    0x11,
    0xFF,
    0x0D,
    0x3D,
    0x00,
    0x01,
    0xFF,
    0x00,
    0x00,
    0x00,
    0x00,
    0x64,
    0x64,
    0x64,
]

OFF = [
    0x11,
    0xFF,
    0x0D,
    0x3D,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x64,
    0x64,
    0x64,
]

COMMIT = [0x11, 0xFF, 0x0C, 0x5A]


def pad(packet: list[int]) -> bytes:
    return bytes(packet + [0x00] * (20 - len(packet)))


def find_keyboard() -> dict:
    for device in hid.enumerate(VID, PID):
        if device.get("usage_page") == USAGE_PAGE and device.get("usage") == USAGE:
            return device
    raise RuntimeError("Logitech G610 usage 0x0604 not found")


def write_state(device: hid.device, packet: list[int]) -> None:
    device.write(pad(packet))
    device.write(pad(COMMIT))


def main() -> int:
    stop = False

    def handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    keyboard = find_keyboard()
    device = hid.device()
    device.open_path(keyboard["path"])

    try:
        while not stop:
            write_state(device, ON)
            time.sleep(HALF_PERIOD_SECONDS)
            write_state(device, OFF)
            time.sleep(HALF_PERIOD_SECONDS)
        write_state(device, OFF)
    finally:
        device.close()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"g610_blink10.py: {error}", file=sys.stderr)
        raise SystemExit(1)
