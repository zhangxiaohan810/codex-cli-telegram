#!/usr/bin/env python3
"""Interactive notification setup for Codex approval requests."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = {
    "NOTIFICATION_CHANNEL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
}


def ask_choice(prompt: str, choices: list[str], default: str) -> str:
    labels = "/".join(f"{choice}{'*' if choice == default else ''}" for choice in choices)
    while True:
        answer = input(f"{prompt} [{labels}]: ").strip().lower()
        if not answer:
            return default
        matches = [choice for choice in choices if choice.startswith(answer)]
        if len(matches) == 1:
            return matches[0]
        print(f"Choose one of: {', '.join(choices)}")


def ask_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def update_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    keys = ENV_KEYS | set(values)
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in keys]
    kept.extend(f"{key}={value}" for key, value in values.items() if value != "")
    path.write_text("\n".join(kept) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=["telegram", "keyboard", "both"], help="Approval notification channel")
    parser.add_argument("--telegram-token", help="Telegram bot token")
    parser.add_argument("--telegram-chat-id", help="Telegram chat id")
    parser.add_argument("--g610-args", default="", help="Extra args passed to codex-g610-setup when keyboard is enabled")
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="Bridge .env path")
    args = parser.parse_args()

    channel = args.channel or ask_choice("Approval notification method?", ["telegram", "keyboard", "both"], "both")
    values = {"NOTIFICATION_CHANNEL": channel}

    if channel in {"telegram", "both"}:
        values["TELEGRAM_BOT_TOKEN"] = args.telegram_token or ask_text("Telegram bot token")
        values["TELEGRAM_CHAT_ID"] = args.telegram_chat_id or ask_text("Telegram chat id")

    update_env(Path(args.env_file), values)
    print(f"Updated {args.env_file} notification channel: {channel}")

    if channel in {"keyboard", "both"}:
        command = ["python", str(ROOT / "scripts" / "setup_g610_approval_hook.py")]
        if args.g610_args:
            command.extend(args.g610_args.split())
        subprocess.run(command, check=True)

    print()
    print("Restart codex-telegram for the new notification settings to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
