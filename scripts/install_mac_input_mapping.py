#!/usr/bin/env python3
"""Install a local macOS input remapping service for Codex users.

This avoids Karabiner/LinearMouse so it will not compete for exclusive device
access with the Logitech G610 lighting controller.
"""

from __future__ import annotations

import json
from pathlib import Path


DEFAULT_KEYS = ["c", "v", "b", "z", "s", "a", "x", "f", "p", "n", "w", "t"]


SERVICE_SOURCE = r"""#!/usr/bin/env python3
from __future__ import annotations

import json
import signal
import sys
from pathlib import Path

from AppKit import NSEvent
from Cocoa import NSRunLoop
from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CGEventCreateKeyboardEvent,
    CGEventGetFlags,
    CGEventGetIntegerValueField,
    CGEventKeyboardSetUnicodeString,
    CGEventMaskBit,
    CGEventPost,
    CGEventSetFlags,
    CGEventSetIntegerValueField,
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventTapLocation,
    CGEventTapOptionDefault,
    CGEventTapPlacementHeadInsertEventTap,
    CGEventType,
    kCFRunLoopCommonModes,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskControl,
    kCGEventKeyDown,
    kCGEventKeyUp,
    kCGEventScrollWheel,
    kCGHIDEventTap,
    kCGKeyboardEventKeycode,
    kCGScrollWheelEventDeltaAxis1,
    kCGScrollWheelEventFixedPtDeltaAxis1,
    kCGScrollWheelEventIsContinuous,
    kCGSessionEventTap,
)


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
    mouse = data.get("mouse", {})
    keys = {KEYCODES[k]: k for k in keyboard.get("keys", []) if k in KEYCODES}
    return {
        "keys": keys,
        "reverse_scroll": bool(mouse.get("reverse_scroll", True)),
        "discrete_only": bool(mouse.get("reverse_scroll_discrete_only", True)),
    }


CFG = load_config()


def should_flip_scroll(event) -> bool:
    if not CFG["reverse_scroll"]:
        return False
    if not CFG["discrete_only"]:
        return True
    return not bool(CGEventGetIntegerValueField(event, kCGScrollWheelEventIsContinuous))


def emit_command_key(keycode: int, is_down: bool) -> None:
    event = CGEventCreateKeyboardEvent(None, keycode, is_down)
    CGEventSetFlags(event, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, event)


def callback(_proxy, event_type, event, _refcon):
    if not STATE["enabled"]:
        return event

    if event_type in (kCGEventKeyDown, kCGEventKeyUp):
        flags = CGEventGetFlags(event)
        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
        if flags & kCGEventFlagMaskControl and keycode in CFG["keys"]:
            emit_command_key(keycode, event_type == kCGEventKeyDown)
            return None

    if event_type == kCGEventScrollWheel and should_flip_scroll(event):
        axis1 = CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1)
        CGEventSetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1, -axis1)
        fixed_axis1 = CGEventGetIntegerValueField(event, kCGScrollWheelEventFixedPtDeltaAxis1)
        CGEventSetIntegerValueField(event, kCGScrollWheelEventFixedPtDeltaAxis1, -fixed_axis1)
        return event

    return event


def handle_stop(_signum, _frame):
    sys.exit(0)


def main() -> int:
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    mask = (
        CGEventMaskBit(kCGEventKeyDown)
        | CGEventMaskBit(kCGEventKeyUp)
        | CGEventMaskBit(kCGEventScrollWheel)
    )
    tap = CGEventTapCreate(
        kCGSessionEventTap,
        CGEventTapPlacementHeadInsertEventTap,
        CGEventTapOptionDefault,
        mask,
        callback,
        None,
    )
    if tap is None:
        print("Failed to create event tap. Enable Input Monitoring and Accessibility for the terminal or Python process.", file=sys.stderr)
        return 1
    source = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
    CGEventTapEnable(tap, True)
    NSRunLoop.currentRunLoop().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


START_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail

PY="$HOME/miniforge3/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi

SERVICE="$HOME/.codex-g610/input_mapper.py"
PIDFILE="$HOME/.codex-g610/input-mapper.pid"
LOG="$HOME/.codex-g610/input-mapper.log"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running pid $(cat "$PIDFILE")"
  exit 0
fi

nohup "$PY" "$SERVICE" >"$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 0.5

if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
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
- Reverses discrete mouse wheel direction by default
- Uses a macOS event tap instead of Karabiner/LinearMouse

Start:
  ~/bin/codex-mac-input-start

Stop:
  ~/bin/codex-mac-input-stop

Status:
  ~/bin/codex-mac-input-status

Permissions required:
- Input Monitoring
- Accessibility

Edit mapping.json to customize the mapping logic.
"""


def main() -> int:
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
                "mouse": {
                    "default": "Reverse external mouse wheel direction.",
                    "reverse_scroll": True,
                    "reverse_scroll_discrete_only": True,
                },
            },
            indent=2,
        )
        + "\n"
    )
    service_path.write_text(SERVICE_SOURCE)
    readme_path.write_text(README)
    start_path.write_text(START_SCRIPT)
    stop_path.write_text(STOP_SCRIPT)
    status_path.write_text(STATUS_SCRIPT)

    service_path.chmod(0o755)
    start_path.chmod(0o755)
    stop_path.chmod(0o755)
    status_path.chmod(0o755)

    print("Installed macOS local input mapper:")
    print(f"  {mapping_path}")
    print(f"  {service_path}")
    print(f"  {readme_path}")
    print("Commands:")
    print("  ~/bin/codex-mac-input-start")
    print("  ~/bin/codex-mac-input-stop")
    print("  ~/bin/codex-mac-input-status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
