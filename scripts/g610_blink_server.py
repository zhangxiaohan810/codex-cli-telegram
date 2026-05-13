#!/usr/bin/env python3
"""Local Logitech G610 blink controller.

Run this on the Mac that owns the keyboard, from a local Terminal session:

    sudo python scripts/g610_blink_server.py

Then remote machines can trigger it over SSH without opening HID themselves:

    ssh Tiezhu@mac 'printf start | nc 127.0.0.1 19610'
    ssh Tiezhu@mac 'printf stop | nc 127.0.0.1 19610'
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


class G610Controller:
    def __init__(self) -> None:
        self.device = hid.device()
        self.device.open_path(self.find_keyboard()["path"])
        self.lock = threading.Lock()
        self.blinking = threading.Event()
        self.stopped = threading.Event()
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
        while not self.stopped.is_set():
            if not self.blinking.is_set():
                time.sleep(0.05)
                continue
            self.write_state(ON)
            time.sleep(HALF_PERIOD_SECONDS)
            self.write_state(OFF)
            time.sleep(HALF_PERIOD_SECONDS)

    def start(self) -> None:
        self.blinking.set()

    def stop(self) -> None:
        self.blinking.clear()
        self.write_state(OFF)

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
                if command == "start":
                    controller.start()
                    connection.sendall(b"ok start\n")
                elif command == "stop":
                    controller.stop()
                    connection.sendall(b"ok stop\n")
                elif command == "status":
                    state = b"blinking" if controller.blinking.is_set() else b"idle"
                    connection.sendall(b"ok " + state + b"\n")
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
