#!/usr/bin/env python3
"""Install optional macOS keyboard/mouse mapping templates for external devices."""

from __future__ import annotations

import json
from pathlib import Path


DEFAULT_KEYS = ["c", "v", "b", "z", "s", "a", "x", "f", "p", "n", "w", "t"]


def karabiner_rule(keys: list[str]) -> dict:
    manipulators = []
    for key in keys:
        manipulators.append(
            {
                "type": "basic",
                "from": {
                    "key_code": key,
                    "modifiers": {
                        "mandatory": ["left_control"],
                        "optional": ["any"],
                    },
                },
                "to": [
                    {
                        "key_code": key,
                        "modifiers": ["left_command"],
                    }
                ],
                "conditions": [
                    {
                        "type": "device_if",
                        "identifiers": [
                            {
                                "vendor_id": 1133,
                                "product_id": 49976,
                                "description": "Logitech G610",
                            }
                        ],
                    }
                ],
            }
        )
    return {
        "title": "Codex external keyboard Mac shortcuts",
        "rules": [
            {
                "description": "External Logitech G610: map Ctrl+C/V/B/Z/S/... to Command+C/V/B/Z/S/...",
                "manipulators": manipulators,
            }
        ],
    }


def main() -> int:
    base = Path.home() / ".codex-g610"
    base.mkdir(parents=True, exist_ok=True)

    karabiner_path = base / "karabiner-external-keyboard-mac-shortcuts.json"
    mapping_path = base / "mapping.json"
    readme_path = base / "README.txt"

    karabiner_path.write_text(json.dumps(karabiner_rule(DEFAULT_KEYS), indent=2) + "\n")
    mapping_path.write_text(
        json.dumps(
            {
                "keyboard": {
                    "default": "Map external Logitech G610 Ctrl+keys to Command+keys.",
                    "keys": DEFAULT_KEYS,
                    "karabiner_complex_modification": str(karabiner_path),
                },
                "mouse": {
                    "default": "Reverse external mouse wheel direction.",
                    "recommended_tool": "LinearMouse",
                    "note": "Use LinearMouse per-device scrolling so the trackpad is not affected.",
                },
            },
            indent=2,
        )
        + "\n"
    )
    readme_path.write_text(
        "\n".join(
            [
                "Codex G610 input mapping templates",
                "",
                "Keyboard:",
                "1. Install Karabiner-Elements.",
                "2. Copy or import this complex modification:",
                f"   {karabiner_path}",
                "3. Enable the rule named 'External Logitech G610: map Ctrl+C/V/B/Z/S/...'.",
                "",
                "Mouse:",
                "1. Install LinearMouse.",
                "2. Select the external mouse device.",
                "3. Enable reverse scrolling for the external mouse only.",
                "",
                "Edit mapping.json first if you want a different mapping logic.",
                f"Mapping config: {mapping_path}",
            ]
        )
        + "\n"
    )

    print("Installed macOS input mapping templates:")
    print(f"  {mapping_path}")
    print(f"  {karabiner_path}")
    print(f"  {readme_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
