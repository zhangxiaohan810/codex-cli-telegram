#!/usr/bin/env python3
"""Local Logitech G610 blink controller.

Run this on the Mac that owns the keyboard, from a local Terminal session:

    sudo python scripts/g610_blink_server.py

Then remote machines can trigger it over SSH without opening HID themselves:

    ssh your_mac_user@mac 'printf start | nc 127.0.0.1 19610'
    ssh your_mac_user@mac 'printf stop | nc 127.0.0.1 19610'
"""

from __future__ import annotations

import signal
import socket
import sys
import threading
import time

import hid


VID = 0x046D
PID = 0xC338
USAGE_PAGE = 0xFF43
USAGE = 0x0604
HOST = "127.0.0.1"
PORT = 19610
DEFAULT_IDLE_BRIGHTNESS = 0
DEFAULT_BLINK_BRIGHTNESS = 100
DEFAULT_FREQUENCY_HZ = 3.0
MIN_FREQUENCY_HZ = 0.5
MAX_FREQUENCY_HZ = 10.0
DEFAULT_BURST_SECONDS = 5.0
DEFAULT_PAUSE_SECONDS = 15.0
MIN_BURST_SECONDS = 1.0
MAX_BURST_SECONDS = 60.0
MIN_PAUSE_SECONDS = 0.0
MAX_PAUSE_SECONDS = 120.0

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


def clamp_brightness(value: int) -> int:
    return max(0, min(100, value))


def packet_for_brightness(brightness: int) -> list[int]:
    brightness = clamp_brightness(brightness)
    if brightness <= 0:
        return OFF.copy()
    packet = ON.copy()
    packet[6] = round(0xFF * brightness / 100)
    return packet


def clamp_frequency(value: float) -> float:
    return max(MIN_FREQUENCY_HZ, min(MAX_FREQUENCY_HZ, value))


def clamp_burst_seconds(value: float) -> float:
    return max(MIN_BURST_SECONDS, min(MAX_BURST_SECONDS, value))


def clamp_pause_seconds(value: float) -> float:
    return max(MIN_PAUSE_SECONDS, min(MAX_PAUSE_SECONDS, value))


class G610Controller:
    def __init__(self) -> None:
        self.device = hid.device()
        self.device.open_path(self.find_keyboard()["path"])
        self.lock = threading.Lock()
        self.blinking = threading.Event()
        self.stopped = threading.Event()
        self.default_brightness = DEFAULT_IDLE_BRIGHTNESS
        self.blink_brightness = DEFAULT_BLINK_BRIGHTNESS
        self.frequency_hz = DEFAULT_FREQUENCY_HZ
        self.burst_seconds = DEFAULT_BURST_SECONDS
        self.pause_seconds = DEFAULT_PAUSE_SECONDS
        self.thread = threading.Thread(target=self.blink_loop, daemon=True)
        self.thread.start()

    def find_keyboard(self) -> dict:
        for device in hid.enumerate(VID, PID):
            if device.get("usage_page") == USAGE_PAGE and device.get("usage") == USAGE:
                return device
        raise RuntimeError("Logitech G610 usage 0x0604 not found")

    def write_state(self, packet: list[int]) -> None:
        with self.lock:
            self.device.write(pad(packet))
            self.device.write(pad(COMMIT))

    def blink_loop(self) -> None:
        next_tick = time.monotonic()
        cycle_start = time.monotonic()
        active_phase: bool | None = None
        on = True
        while not self.stopped.is_set():
            if not self.blinking.is_set():
                on = True
                next_tick = time.monotonic()
                cycle_start = next_tick
                active_phase = None
                time.sleep(0.01)
                continue

            now = time.monotonic()
            cycle_seconds = self.burst_seconds + self.pause_seconds
            elapsed = (now - cycle_start) % cycle_seconds
            is_active = elapsed < self.burst_seconds

            if not is_active:
                if active_phase is not False:
                    self.write_state(packet_for_brightness(self.default_brightness))
                    active_phase = False
                next_tick = time.monotonic()
                sleep_for = min(0.1, max(0.01, cycle_seconds - elapsed))
                time.sleep(sleep_for)
                continue

            if active_phase is not True:
                on = True
                next_tick = now
                active_phase = True

            brightness = self.blink_brightness if on else self.default_brightness
            self.write_state(packet_for_brightness(brightness))
            on = not on
            next_tick += 1.0 / (self.frequency_hz * 2.0)
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()

    def start(self) -> None:
        self.blinking.set()

    def stop(self) -> None:
        self.blinking.clear()
        self.write_state(packet_for_brightness(self.default_brightness))

    def set_default_brightness(self, brightness: int) -> None:
        self.default_brightness = clamp_brightness(brightness)
        if not self.blinking.is_set():
            self.write_state(packet_for_brightness(self.default_brightness))

    def set_blink_brightness(self, brightness: int) -> None:
        self.blink_brightness = clamp_brightness(brightness)

    def set_frequency(self, frequency_hz: float) -> None:
        self.frequency_hz = clamp_frequency(frequency_hz)

    def set_burst_seconds(self, seconds: float) -> None:
        self.burst_seconds = clamp_burst_seconds(seconds)

    def set_pause_seconds(self, seconds: float) -> None:
        self.pause_seconds = clamp_pause_seconds(seconds)

    def status(self) -> str:
        state = "blinking" if self.blinking.is_set() else "idle"
        return (
            f"ok {state} default={self.default_brightness} "
            f"blink={self.blink_brightness} frequency={self.frequency_hz:.1f} "
            f"burst={self.burst_seconds:.1f} pause={self.pause_seconds:.1f}\n"
        )

    def close(self) -> None:
        self.stopped.set()
        self.stop()
        self.thread.join(timeout=1)
        self.device.close()


def serve(controller: G610Controller) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(8)
        server.settimeout(0.5)
        print(f"g610 blink server listening on {HOST}:{PORT}", flush=True)

        while not should_exit.is_set():
            try:
                connection, _addr = server.accept()
            except TimeoutError:
                continue
            with connection:
                command = connection.recv(64).decode("utf-8", "replace").strip().lower()
                parts = command.split()
                if command == "start":
                    controller.start()
                    connection.sendall(b"ok start\n")
                elif command == "stop":
                    controller.stop()
                    connection.sendall(b"ok stop\n")
                elif command == "status":
                    connection.sendall(controller.status().encode("utf-8"))
                elif len(parts) == 3 and parts[0] == "set" and parts[1] == "default-brightness":
                    try:
                        controller.set_default_brightness(int(parts[2]))
                        connection.sendall(controller.status().encode("utf-8"))
                    except ValueError:
                        connection.sendall(b"error invalid brightness\n")
                elif len(parts) == 3 and parts[0] == "set" and parts[1] == "blink-brightness":
                    try:
                        controller.set_blink_brightness(int(parts[2]))
                        connection.sendall(controller.status().encode("utf-8"))
                    except ValueError:
                        connection.sendall(b"error invalid brightness\n")
                elif len(parts) == 3 and parts[0] == "set" and parts[1] == "frequency":
                    try:
                        controller.set_frequency(float(parts[2]))
                        connection.sendall(controller.status().encode("utf-8"))
                    except ValueError:
                        connection.sendall(b"error invalid frequency\n")
                elif len(parts) == 3 and parts[0] == "set" and parts[1] == "burst-seconds":
                    try:
                        controller.set_burst_seconds(float(parts[2]))
                        connection.sendall(controller.status().encode("utf-8"))
                    except ValueError:
                        connection.sendall(b"error invalid burst seconds\n")
                elif len(parts) == 3 and parts[0] == "set" and parts[1] == "pause-seconds":
                    try:
                        controller.set_pause_seconds(float(parts[2]))
                        connection.sendall(controller.status().encode("utf-8"))
                    except ValueError:
                        connection.sendall(b"error invalid pause seconds\n")
                elif command == "quit":
                    connection.sendall(b"ok quit\n")
                    should_exit.set()
                else:
                    connection.sendall(b"error unknown command\n")


should_exit = threading.Event()


def handle_stop(_signum: int, _frame: object) -> None:
    should_exit.set()


def main() -> int:
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    controller = G610Controller()
    try:
        serve(controller)
    finally:
        controller.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"g610_blink_server.py: {error}", file=sys.stderr)
        raise SystemExit(1)
