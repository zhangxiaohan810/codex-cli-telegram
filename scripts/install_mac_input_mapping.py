#!/usr/bin/env python3
"""Install a local macOS input remapping service for Codex users.

This avoids Karabiner/LinearMouse so it will not compete for exclusive device
access with the Logitech G610 lighting controller.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_KEYS = ["c", "v", "b", "z", "s", "a", "x", "f", "p", "n", "w", "t"]


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
SYNTHETIC_SCROLL_TAG = 0x434F444558474631


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
    return not bool(
        Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGScrollWheelEventIsContinuous
        )
    )


def get_scroll_value(event, field) -> int:
    if not hasattr(Quartz, field):
        return 0
    return int(Quartz.CGEventGetIntegerValueField(event, getattr(Quartz, field)))


def is_synthetic_scroll_event(event) -> bool:
    if not hasattr(Quartz, "kCGEventSourceUserData"):
        return False
    return (
        Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData)
        == SYNTHETIC_SCROLL_TAG
    )


def build_reversed_scroll_event(event):
    line_v = -get_scroll_value(event, "kCGScrollWheelEventDeltaAxis1")
    line_h = -get_scroll_value(event, "kCGScrollWheelEventDeltaAxis2")
    fixed_v = -get_scroll_value(event, "kCGScrollWheelEventFixedPtDeltaAxis1")
    fixed_h = -get_scroll_value(event, "kCGScrollWheelEventFixedPtDeltaAxis2")
    point_v = -get_scroll_value(event, "kCGScrollWheelEventPointDeltaAxis1")
    point_h = -get_scroll_value(event, "kCGScrollWheelEventPointDeltaAxis2")
    is_continuous = bool(
        get_scroll_value(event, "kCGScrollWheelEventIsContinuous")
    )

    units = (
        Quartz.kCGScrollEventUnitPixel
        if is_continuous
        else Quartz.kCGScrollEventUnitLine
    )
    primary = point_v if is_continuous and point_v else line_v
    secondary = point_h if is_continuous and point_h else line_h
    wheel_count = 2 if secondary else 1

    new_event = Quartz.CGEventCreateScrollWheelEvent(
        None, units, wheel_count, primary, secondary
    )
    if new_event is None:
        return None

    Quartz.CGEventSetFlags(new_event, Quartz.CGEventGetFlags(event))

    if hasattr(Quartz, "kCGEventSourceUserData"):
        Quartz.CGEventSetIntegerValueField(
            new_event, Quartz.kCGEventSourceUserData, SYNTHETIC_SCROLL_TAG
        )

    if hasattr(Quartz, "kCGScrollWheelEventIsContinuous"):
        Quartz.CGEventSetIntegerValueField(
            new_event,
            Quartz.kCGScrollWheelEventIsContinuous,
            1 if is_continuous else 0,
        )

    for field, value in (
        ("kCGScrollWheelEventDeltaAxis1", line_v),
        ("kCGScrollWheelEventDeltaAxis2", line_h),
        ("kCGScrollWheelEventFixedPtDeltaAxis1", fixed_v),
        ("kCGScrollWheelEventFixedPtDeltaAxis2", fixed_h),
        ("kCGScrollWheelEventPointDeltaAxis1", point_v),
        ("kCGScrollWheelEventPointDeltaAxis2", point_h),
    ):
        if value and hasattr(Quartz, field):
            Quartz.CGEventSetIntegerValueField(
                new_event, getattr(Quartz, field), value
            )

    return new_event


def emit_command_key(keycode: int, is_down: bool) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(None, keycode, is_down)
    Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def callback(_proxy, event_type, event, _refcon):
    if not STATE["enabled"]:
        return event

    if event_type == Quartz.kCGEventScrollWheel and is_synthetic_scroll_event(event):
        return event

    if event_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
        flags = Quartz.CGEventGetFlags(event)
        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode
        )
        if flags & Quartz.kCGEventFlagMaskControl and keycode in CFG["keys"]:
            emit_command_key(keycode, event_type == Quartz.kCGEventKeyDown)
            return None

    if event_type == Quartz.kCGEventScrollWheel and should_flip_scroll(event):
        new_event = build_reversed_scroll_event(event)
        if new_event is None:
            return event
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, new_event)
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
        | Quartz.CGEventMaskBit(Quartz.kCGEventScrollWheel)
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
    start_path.write_text(START_SCRIPT_TEMPLATE.format(python_path=args.python_path))
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
