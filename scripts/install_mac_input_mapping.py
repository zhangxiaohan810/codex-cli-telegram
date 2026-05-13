#!/usr/bin/env python3
"""Install a local macOS input remapping service for Codex users.

This avoids Karabiner/LinearMouse so it will not compete for exclusive device
access with the Logitech G610 lighting controller.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
from pathlib import Path


DEFAULT_KEYS = ["c", "v", "b", "z", "s", "a", "x", "f", "p", "n", "w", "t"]
APPLE_VENDOR_ID = 0x05AC


def detect_pointing_devices_hid() -> list[dict[str, object]]:
    try:
        import hid  # type: ignore
    except Exception:
        return []

    devices: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()

    for device in hid.enumerate():
        if device.get("usage_page") != 0x1 or device.get("usage") != 0x2:
            continue
        vendor_id = int(device.get("vendor_id") or 0)
        product_id = int(device.get("product_id") or 0)
        if not vendor_id or not product_id or vendor_id == APPLE_VENDOR_ID:
            continue
        key = (vendor_id, product_id)
        if key in seen:
            continue
        seen.add(key)
        devices.append(
            {
                "vendor_id": vendor_id,
                "product_id": product_id,
                "manufacturer": device.get("manufacturer_string") or "",
                "product": device.get("product_string") or "",
            }
        )

    return devices


def detect_pointing_devices_ioreg() -> list[dict[str, object]]:
    try:
        result = subprocess.run(
            ["ioreg", "-a", "-r", "-c", "IOHIDDevice"],
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    try:
        records = plistlib.loads(result.stdout)
    except Exception:
        return []

    devices: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()

    for record in records:
        usage_page = int(record.get("PrimaryUsagePage") or 0)
        usage = int(record.get("PrimaryUsage") or 0)
        vendor_id = int(record.get("VendorID") or 0)
        product_id = int(record.get("ProductID") or 0)
        transport = str(record.get("Transport") or "")
        if usage_page != 0x1 or usage != 0x2:
            continue
        if not vendor_id or not product_id or vendor_id == APPLE_VENDOR_ID:
            continue
        if transport and transport not in {"USB", "Bluetooth"}:
            continue
        key = (vendor_id, product_id)
        if key in seen:
            continue
        seen.add(key)
        devices.append(
            {
                "vendor_id": vendor_id,
                "product_id": product_id,
                "manufacturer": str(record.get("Manufacturer") or ""),
                "product": str(record.get("Product") or ""),
            }
        )

    return devices


def detect_pointing_devices() -> list[dict[str, object]]:
    devices = detect_pointing_devices_hid()
    if devices:
        return devices
    return detect_pointing_devices_ioreg()


def find_selected_profile(data: dict) -> dict:
    profiles = data.setdefault("profiles", [])
    if not profiles:
        raise RuntimeError("Karabiner config has no profiles.")
    for profile in profiles:
        if profile.get("selected"):
            return profile
    return profiles[0]


def configure_karabiner_mouse_flip(devices: list[dict[str, object]]) -> tuple[Path, list[dict[str, object]]]:
    config_path = Path.home() / ".config" / "karabiner" / "karabiner.json"
    if not config_path.exists():
        return config_path, []

    original = config_path.read_text()
    data = json.loads(original)
    profile = find_selected_profile(data)
    profile_devices = profile.setdefault("devices", [])
    updated: list[dict[str, object]] = []

    for target in devices:
        vendor_id = int(target["vendor_id"])
        product_id = int(target["product_id"])
        match = None
        for device in profile_devices:
            identifiers = device.get("identifiers", {})
            if (
                identifiers.get("vendor_id") == vendor_id
                and identifiers.get("product_id") == product_id
                and identifiers.get("is_pointing_device") is True
            ):
                match = device
                break
        if match is None:
            match = {
                "identifiers": {
                    "vendor_id": vendor_id,
                    "product_id": product_id,
                    "is_keyboard": False,
                    "is_pointing_device": True,
                }
            }
            profile_devices.append(match)

        match["mouse_flip_vertical_wheel"] = True
        match.setdefault("mouse_flip_horizontal_wheel", False)
        match.setdefault("mouse_swap_wheels", False)
        updated.append(target)

    backup_path = config_path.with_name("karabiner.json.codex-backup")
    if not backup_path.exists():
        backup_path.write_text(original)

    config_path.write_text(json.dumps(data, indent=2) + "\n")
    return config_path, updated


SERVICE_SOURCE = r"""#!/usr/bin/env python3
from __future__ import annotations

import json
import signal
import sys
from pathlib import Path

from Cocoa import NSRunLoop
import Quartz


KEYCODES = {
    "a": 0,
    "b": 11,
    "c": 8,
    "f": 3,
    "n": 45,
    "p": 35,
    "s": 1,
    "t": 17,
    "v": 9,
    "w": 13,
    "x": 7,
    "z": 6,
}


CONFIG = Path.home() / ".codex-g610" / "mapping.json"
STATE = {"enabled": True}


def load_config() -> dict:
    data = json.loads(CONFIG.read_text())
    keyboard = data.get("keyboard", {})
    keys = {KEYCODES[k]: k for k in keyboard.get("keys", []) if k in KEYCODES}
    return {"keys": keys}


CFG = load_config()


def emit_command_key(keycode: int, is_down: bool) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(None, keycode, is_down)
    Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def callback(_proxy, event_type, event, _refcon):
    if not STATE["enabled"]:
        return event

    if event_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
        flags = Quartz.CGEventGetFlags(event)
        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode
        )
        if flags & Quartz.kCGEventFlagMaskControl and keycode in CFG["keys"]:
            emit_command_key(keycode, event_type == Quartz.kCGEventKeyDown)
            return None

    return event


def handle_stop(_signum, _frame):
    sys.exit(0)


def main() -> int:
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    mask = (
        Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
    )
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionDefault,
        mask,
        callback,
        None,
    )
    if tap is None:
        print("Failed to create event tap. Enable Input Monitoring and Accessibility for the terminal or Python process.", file=sys.stderr)
        return 1
    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
    )
    Quartz.CGEventTapEnable(tap, True)
    NSRunLoop.currentRunLoop().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


START_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
set -euo pipefail

PY="{python_path}"

SERVICE="$HOME/.codex-g610/input_mapper.py"
PIDFILE="$HOME/.codex-g610/input-mapper.pid"
LOG="$HOME/.codex-g610/input-mapper.log"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running pid $(cat "$PIDFILE")"
  exit 0
fi

if ! "$PY" -c 'from Cocoa import NSRunLoop; import Quartz; print(Quartz.CGEventTapCreate)' >/dev/null 2>&1; then
  echo "Missing pyobjc in $PY" >&2
  echo "Install it with:" >&2
  echo "  $PY -m pip install pyobjc" >&2
  exit 1
fi

nohup "$PY" "$SERVICE" >"$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 1

if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  cat "$LOG" >&2
  rm -f "$PIDFILE"
  exit 1
fi

if ps -o stat= -p "$(cat "$PIDFILE")" 2>/dev/null | grep -q '^Z'; then
  cat "$LOG" >&2
  rm -f "$PIDFILE"
  exit 1
fi

echo "started pid $(cat "$PIDFILE")"
"""


STOP_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail

PIDFILE="$HOME/.codex-g610/input-mapper.pid"

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
  fi
  rm -f "$PIDFILE"
fi

echo "stopped"
"""


STATUS_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail

PIDFILE="$HOME/.codex-g610/input-mapper.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "running pid $(cat "$PIDFILE")"
else
  echo "stopped"
fi
"""


README = """Codex G610 local input mapper

What it does:
- Maps external keyboard Ctrl+C/V/B/Z/S/A/X/F/P/N/W/T to Command+...
- Uses a macOS event tap for keyboard mapping only
- Mouse wheel reversal is configured through Karabiner when available

Start:
  ~/bin/codex-mac-input-start

Stop:
  ~/bin/codex-mac-input-stop

Status:
  ~/bin/codex-mac-input-status

Permissions required:
- Input Monitoring
- Accessibility

Edit mapping.json to customize keyboard mapping logic.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-path", default=str(Path.home() / "miniforge3" / "bin" / "python"))
    args = parser.parse_args()

    base = Path.home() / ".codex-g610"
    base.mkdir(parents=True, exist_ok=True)
    bin_dir = Path.home() / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    mapping_path = base / "mapping.json"
    service_path = base / "input_mapper.py"
    readme_path = base / "README.txt"
    start_path = bin_dir / "codex-mac-input-start"
    stop_path = bin_dir / "codex-mac-input-stop"
    status_path = bin_dir / "codex-mac-input-status"

    mapping_path.write_text(
        json.dumps(
            {
                "keyboard": {
                    "default": "Map external keyboard Ctrl+keys to Command+keys.",
                    "keys": DEFAULT_KEYS,
                },
            },
            indent=2,
        )
        + "\n"
    )
    service_path.write_text(SERVICE_SOURCE)
    readme_path.write_text(README)
    start_path.write_text(START_SCRIPT_TEMPLATE.format(python_path=args.python_path))
    stop_path.write_text(STOP_SCRIPT)
    status_path.write_text(STATUS_SCRIPT)

    service_path.chmod(0o755)
    start_path.chmod(0o755)
    stop_path.chmod(0o755)
    status_path.chmod(0o755)

    karabiner_path, karabiner_devices = configure_karabiner_mouse_flip(detect_pointing_devices())

    print("Installed macOS local input mapper:")
    print(f"  {mapping_path}")
    print(f"  {service_path}")
    print(f"  {readme_path}")
    print("Commands:")
    print("  ~/bin/codex-mac-input-start")
    print("  ~/bin/codex-mac-input-stop")
    print("  ~/bin/codex-mac-input-status")
    if karabiner_devices:
        print("Configured Karabiner mouse wheel flip for:")
        for device in karabiner_devices:
            label = f'{device.get("manufacturer", "").strip()} {device.get("product", "").strip()}'.strip()
            print(
                f'  {label or "Unknown device"} '
                f'(vid=0x{int(device["vendor_id"]):04x}, pid=0x{int(device["product_id"]):04x})'
            )
        print(f"Karabiner config updated: {karabiner_path}")
        print("Restart Karabiner-Elements or reload its profile to apply mouse wheel changes.")
    else:
        print("Karabiner mouse wheel flip was not configured automatically.")
        print(f"Expected config file: {karabiner_path}")
        print("If Karabiner-Elements is installed, make sure karabiner.json exists and rerun this installer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
