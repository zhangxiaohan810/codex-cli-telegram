#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / ".logs"
KEYBOARD_ACTIONS = {"start", "stop", "status", "test"}
MIN_HOOK_INTERVAL_SECONDS = 0.8
last_hook_at = 0.0
hook_lock = threading.Lock()


def main() -> int:
    raw_args = sys.argv[1:]
    if raw_args[:1] == ["__watch"]:
        return 0

    if not raw_args or raw_args[0] not in KEYBOARD_ACTIONS:
        run_codex(raw_args)

    parser = argparse.ArgumentParser(description="Send keyboard blink commands to the Mac over SSH.")
    parser.add_argument("action", choices=sorted(KEYBOARD_ACTIONS), help="Command to send")
    parser.add_argument("seconds", nargs="?", type=float, default=5.0, help="Blink duration for test mode")
    parser.add_argument("--env-file", default=".env", help="Path to the environment file")
    args = parser.parse_args(raw_args)

    env = load_env(args.env_file)

    if args.action == "test":
        try:
            run_action("start", env)
            print(f"sleep {args.seconds}s", flush=True)
            time.sleep(args.seconds)
        finally:
            run_action("stop", env)
        return 0

    run_action(args.action, env)
    return 0


def run_codex(args: list[str]) -> None:
    env = load_env(".env")
    env["NOTIFICATION_CHANNEL"] = "keyboard"

    codex_bin = env.get("CODEX_BIN", "codex")
    upstream_ws = env.get("CODEX_UPSTREAM_WS", "ws://127.0.0.1:8765")
    bridge_host = env.get("BRIDGE_HOST", "127.0.0.1")
    bridge_port = int(env.get("BRIDGE_PORT", "8766"))
    bridge_url = f"ws://{bridge_host}:{bridge_port}"
    upstream_host, upstream_port = ws_host_port(upstream_ws)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    ensure_listening(
        name="codex app-server",
        host=upstream_host,
        port=upstream_port,
        command=codex_bin,
        args=["app-server", "--listen", upstream_ws],
        env=env,
        log=LOGS_DIR / "codex-app-server.log",
    )
    ensure_listening(
        name="codex keyboard bridge",
        host=bridge_host,
        port=bridge_port,
        command=env.get("CODEX_TELEGRAM_BRIDGE_BIN", "codex-telegram-bridge"),
        args=[],
        env=env,
        cwd=ROOT,
        log=LOGS_DIR / "codex-telegram-bridge.log",
    )

    print(f"[codex-keyboard] starting Codex CLI: {codex_bin} --remote {bridge_url}", flush=True)
    os.execvpe(codex_bin, [codex_bin, "--remote", bridge_url, *args], env)


def ws_host_port(addr: str) -> tuple[str, int]:
    parsed = urlparse(addr)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port is not None:
        return host, parsed.port
    return host, 443 if parsed.scheme == "wss" else 80


def ensure_listening(
    *,
    name: str,
    host: str,
    port: int,
    command: str,
    args: list[str],
    env: dict[str, str],
    log: Path,
    cwd: Path | None = None,
) -> None:
    if can_connect(host, port):
        print(f"[codex-keyboard] {name} already listening on {host}:{port}", flush=True)
        return

    print(f"[codex-keyboard] starting {name} on {host}:{port}", flush=True)
    handle = log.open("ab", buffering=0)
    subprocess.Popen(
        [command, *args],
        cwd=str(cwd or ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=handle,
        start_new_session=True,
    )

    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if can_connect(host, port):
            print(f"[codex-keyboard] {name} ready; log: {log}", flush=True)
            return
        time.sleep(0.3)

    raise RuntimeError(f"{name} did not start on {host}:{port}. Check log: {log}")


def can_connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.7):
            return True
    except OSError:
        return False


def command_for(action: str, env: dict[str, str]) -> list[str] | str:
    key = f"APPROVAL_REQUEST_{action.upper()}_CMD"
    configured = env.get(key, "").strip()
    if configured:
        return expand_env_vars(configured, env)

    host = env.get("MAC_HOST", "").strip()
    user = env.get("MAC_SSH_USER", "Tiezhu").strip()
    port = env.get("MAC_G610_SERVER_PORT", "19610").strip()
    ssh_key = env.get("MAC_SSH_KEY", "").strip()

    if not host:
        raise SystemExit("MAC_HOST is not set and no APPROVAL_REQUEST_*_CMD was configured")

    remote = f"printf {action} | nc 127.0.0.1 {port}"
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if ssh_key:
        command.extend(["-i", ssh_key, "-o", "IdentitiesOnly=yes"])
    command.extend([f"{user}@{host}", remote])
    return command


def describe_command(command: list[str] | str) -> str:
    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


def expand_env_vars(value: str, env: dict[str, str]) -> str:
    output = value
    for key, item in env.items():
        output = output.replace(f"${key}", item)
        output = output.replace(f"${{{key}}}", item)
    return output


def load_env(env_file: str) -> dict[str, str]:
    env = dict(os.environ)
    path = resolve_env_path(env_file)
    if not path.exists():
        return env

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in env:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        env[key] = value
    return env


def resolve_env_path(env_file: str) -> Path:
    path = Path(env_file)
    if path.is_absolute() or env_file != ".env" or path.exists():
        return path
    return Path(__file__).resolve().parents[1] / ".env"


if __name__ == "__main__":
    raise SystemExit(main())
