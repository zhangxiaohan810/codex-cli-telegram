#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import shutil
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / ".logs"
KEYBOARD_ACTIONS = {"start", "stop", "status", "test"}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_LOCAL_G610_PORT = "19610"


def main() -> int:
    raw_args = sys.argv[1:]
    if not raw_args or raw_args[0] not in KEYBOARD_ACTIONS:
        run_codex(raw_args)

    parser = argparse.ArgumentParser(description="Send keyboard blink commands to the Mac over SSH.")
    parser.add_argument("action", choices=sorted(KEYBOARD_ACTIONS), help="Command to send")
    parser.add_argument("seconds", nargs="?", type=float, default=5.0, help="Blink duration for test mode")
    parser.add_argument("--env-file", default=".env", help="Path to the environment file")
    args = parser.parse_args(raw_args)

    env = load_env(args.env_file)
    ensure_default_local_commands(env)

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
    ensure_default_local_commands(env)
    env["NOTIFICATION_CHANNEL"] = "keyboard"

    codex_bin = resolve_codex_bin(env)
    host = env.get("CODEX_KEYBOARD_HOST", env.get("BRIDGE_HOST", DEFAULT_HOST)).strip() or DEFAULT_HOST
    upstream_guard, upstream_port = reserve_port(host)
    bridge_guard, bridge_port = reserve_port(host)
    upstream_ws = f"ws://{host}:{upstream_port}"
    bridge_url = f"ws://{host}:{bridge_port}"

    env["CODEX_UPSTREAM_WS"] = upstream_ws
    env["BRIDGE_HOST"] = host
    env["BRIDGE_PORT"] = str(bridge_port)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    upstream_guard.close()
    ensure_listening(
        name="codex app-server",
        host=host,
        port=upstream_port,
        command=codex_bin,
        args=["app-server", "--listen", upstream_ws],
        env=env,
        log=LOGS_DIR / f"codex-app-server-{upstream_port}.log",
    )

    bridge_guard.close()
    ensure_listening(
        name="codex keyboard bridge",
        host=host,
        port=bridge_port,
        command=resolve_bridge_bin(env),
        args=[],
        env=env,
        cwd=ROOT,
        log=LOGS_DIR / f"codex-keyboard-bridge-{bridge_port}.log",
    )

    print(f"[codex-keyboard] starting Codex CLI: {codex_bin} --remote {bridge_url}", flush=True)
    os.execvpe(codex_bin, [codex_bin, "--remote", bridge_url, *args], env)


def ensure_default_local_commands(env: dict[str, str]) -> None:
    if env.get("APPROVAL_REQUEST_START_CMD", "").strip():
        return

    port = env.get("MAC_G610_SERVER_PORT", DEFAULT_LOCAL_G610_PORT).strip() or DEFAULT_LOCAL_G610_PORT
    env["MAC_G610_SERVER_PORT"] = port
    env.setdefault("APPROVAL_REQUEST_START_CMD", f"printf start | nc 127.0.0.1 {port}")
    env.setdefault("APPROVAL_REQUEST_STOP_CMD", f"printf stop | nc 127.0.0.1 {port}")
    env.setdefault("APPROVAL_REQUEST_STATUS_CMD", f"printf status | nc 127.0.0.1 {port}")


def reserve_port(host: str) -> tuple[socket.socket, int]:
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    guard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    guard.bind((host, 0))
    return guard, int(guard.getsockname()[1])


def resolve_codex_bin(env: dict[str, str]) -> str:
    configured = env.get("CODEX_BIN", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise SystemExit(f"CODEX_BIN is set but not executable: {configured}")

    return resolve_executable(
        "codex",
        env=env,
        extra_candidates=[
            Path.home() / ".local" / "bin" / "codex",
            *sorted((Path.home() / ".nvm" / "versions" / "node").glob("*/bin/codex")),
        ],
        error="Could not find executable codex. Set CODEX_BIN=/full/path/to/codex in .env.",
    )


def resolve_bridge_bin(env: dict[str, str]) -> str:
    configured = env.get("CODEX_TELEGRAM_BRIDGE_BIN", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        resolved = shutil.which(configured, path=env.get("PATH"))
        if resolved:
            return resolved
        raise SystemExit(f"CODEX_TELEGRAM_BRIDGE_BIN is set but not executable: {configured}")

    return resolve_executable(
        "codex-telegram-bridge",
        env=env,
        extra_candidates=[
            ROOT / "bin" / "codex-telegram-bridge.js",
            Path.home() / ".local" / "bin" / "codex-telegram-bridge",
            *sorted((Path.home() / ".nvm" / "versions" / "node").glob("*/bin/codex-telegram-bridge")),
        ],
        error="Could not find executable codex-telegram-bridge.",
    )


def resolve_executable(
    name: str,
    *,
    env: dict[str, str],
    extra_candidates: list[Path],
    error: str,
) -> str:
    script_dir = Path(sys.argv[0]).resolve().parent
    candidates = [script_dir / name, *extra_candidates]

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    resolved = shutil.which(name, path=env.get("PATH"))
    if resolved and Path(resolved).is_file() and os.access(resolved, os.X_OK):
        return resolved

    raise SystemExit(error)


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
        raise RuntimeError(f"{name} port unexpectedly became busy: {host}:{port}")

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
            print(f"[codex-keyboard] {name} ready on {host}:{port}; log: {log}", flush=True)
            return
        time.sleep(0.3)

    raise RuntimeError(f"{name} did not start on {host}:{port}. Check log: {log}")


def can_connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.7):
            return True
    except OSError:
        return False


def run_action(action: str, env: dict[str, str]) -> None:
    command = command_for(action, env)
    print(f"{action}: {describe_command(command)}", flush=True)
    subprocess.run(command, check=True, env=env, shell=isinstance(command, str))


def command_for(action: str, env: dict[str, str]) -> list[str] | str:
    key = f"APPROVAL_REQUEST_{action.upper()}_CMD"
    configured = env.get(key, "").strip()
    if configured:
        return expand_env_vars(configured, env)

    host = env.get("MAC_HOST", "").strip()
    user = env.get("MAC_SSH_USER", os.environ.get("USER", "macuser")).strip()
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
    return ROOT / ".env"


if __name__ == "__main__":
    raise SystemExit(main())
