#!/usr/bin/env python3
"""Interactive notification setup for Codex approval requests."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = {
    "NOTIFICATION_CHANNEL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
}


def default_env_file() -> Path:
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    nested_env = Path.cwd() / "codex-cli-telegram" / ".env"
    if nested_env.exists():
        return nested_env
    return ROOT / ".env"


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


def load_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def telegram_get_updates(token: str, proxy: str = "") -> dict:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    command = ["curl", "-fsSL", "--max-time", "10"]
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(url)
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def extract_chat_id(updates: dict) -> str:
    for item in reversed(updates.get("result", [])):
        message = item.get("message") or item.get("edited_message") or item.get("channel_post")
        chat_id = message and message.get("chat", {}).get("id")
        if chat_id is not None:
            return str(chat_id)
    return ""


def resolve_telegram_chat_id(token: str, existing_chat_id: str = "", proxy: str = "") -> str:
    if existing_chat_id:
        print(f"Using existing Telegram chat id: {existing_chat_id}")
        return existing_chat_id

    for attempt in range(2):
        try:
            chat_id = extract_chat_id(telegram_get_updates(token, proxy))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"Could not call Telegram getUpdates: {error}")
            break
        if chat_id:
            print(f"Detected Telegram chat id: {chat_id}")
            return chat_id
        if attempt == 0:
            print("No Telegram chat found yet. Send any message to the new bot, then press Enter.")
            input()

    return ask_text("Telegram chat id")


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
    parser.add_argument("--env-file", default=None, help="Bridge .env path")
    args = parser.parse_args()

    env_file = Path(args.env_file) if args.env_file else default_env_file()
    existing = load_env(env_file)

    channel = args.channel or existing.get("NOTIFICATION_CHANNEL") or ask_choice("Approval notification method?", ["telegram", "keyboard", "both"], "both")
    values = {"NOTIFICATION_CHANNEL": channel}

    if channel in {"telegram", "both"}:
        token = args.telegram_token or existing.get("TELEGRAM_BOT_TOKEN") or ask_text("Telegram bot token")
        proxy = existing.get("TELEGRAM_PROXY") or existing.get("HTTPS_PROXY") or existing.get("HTTP_PROXY") or ""
        chat_id = args.telegram_chat_id or resolve_telegram_chat_id(token, existing.get("TELEGRAM_CHAT_ID", ""), proxy)
        values["TELEGRAM_BOT_TOKEN"] = token
        values["TELEGRAM_CHAT_ID"] = chat_id

    update_env(env_file, values)
    print(f"Updated {env_file} notification channel: {channel}")

    if channel in {"keyboard", "both"}:
        command = [sys.executable, str(ROOT / "scripts" / "setup_g610_approval_hook.py"), "--env-file", str(env_file)]
        if args.g610_args:
            command.extend(args.g610_args.split())
        subprocess.run(command, check=True)

    print()
    print("Restart codex-telegram for the new notification settings to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
