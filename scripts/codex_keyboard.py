#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

KEYBOARD_ACTIONS = {"start", "stop", "status", "test"}
CODEX_LOG = Path.home() / ".codex" / "log" / "codex-tui.log"
LOCK_FILE = Path.home() / ".codex" / "codex-keyboard-watch.lock"
MIN_HOOK_INTERVAL_SECONDS = 0.8
last_hook_at = 0.0
hook_lock = threading.Lock()


def main() -> int:
    raw_args = sys.argv[1:]
    if raw_args[:1] == ["__watch"]:
        return run_watcher()

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
    codex_bin = resolve_codex_bin(env)
    ensure_watcher(env)
    print(f"[codex-keyboard] starting Codex CLI: {codex_bin}", flush=True)
    os.execvpe(codex_bin, [codex_bin, *args], env)


def resolve_codex_bin(env: dict[str, str]) -> str:
    configured = env.get("CODEX_BIN", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise SystemExit(f"CODEX_BIN is set but not executable: {configured}")

    candidates = []
    script_dir = Path(sys.argv[0]).resolve().parent
    candidates.append(script_dir / "codex")
    for base in (Path.home() / ".local" / "bin", Path.home() / ".nvm" / "versions" / "node"):
        if base.name == "node" and base.exists():
            candidates.extend(base.glob("*/bin/codex"))
        else:
            candidates.append(base / "codex")

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    resolved = shutil.which("codex", path=env.get("PATH"))
    if resolved and Path(resolved).is_file() and os.access(resolved, os.X_OK):
        return resolved

    raise SystemExit("Could not find executable codex. Set CODEX_BIN=/full/path/to/codex in .env.")


def ensure_watcher(env: dict[str, str]) -> None:
    command = [sys.executable, str(Path(__file__).resolve()), "__watch"]
    subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def run_watcher() -> int:
    lock = try_acquire_lock()
    if lock is None:
        return 0

    env = load_env(".env")
    stop_monitor = threading.Event()
    try:
        monitor_codex_log(env, stop_monitor)
    finally:
        stop_monitor.set()
        run_action_quiet("stop", env)
        release_lock(lock)
    return 0


def run_action(action: str, env: dict[str, str]) -> None:
    command = command_for(action, env)
    print(f"{action}: {describe_command(command)}", flush=True)
    subprocess.run(command, check=True, env=env, shell=isinstance(command, str))


def run_action_quiet(action: str, env: dict[str, str]) -> None:
    threading.Thread(target=run_action_background, args=(action, env), daemon=True).start()


def run_action_background(action: str, env: dict[str, str]) -> None:
    global last_hook_at
    try:
        with hook_lock:
            now = time.monotonic()
            wait_for = MIN_HOOK_INTERVAL_SECONDS - (now - last_hook_at)
            if wait_for > 0:
                time.sleep(wait_for)
            last_hook_at = time.monotonic()
        command = command_for(action, env)
        subprocess.run(
            command,
            check=False,
            env=env,
            shell=isinstance(command, str),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def try_acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        handle.close()
        return None


def release_lock(handle) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def monitor_codex_log(env: dict[str, str], stop_monitor: threading.Event) -> None:
    position = wait_for_log_position(stop_monitor)
    pending_approvals = 0
    last_event = 0.0

    while not stop_monitor.is_set():
        if not CODEX_LOG.exists():
            time.sleep(0.2)
            continue

        with CODEX_LOG.open("r", errors="replace") as handle:
            handle.seek(position)
            while not stop_monitor.is_set():
                line = handle.readline()
                if not line:
                    position = handle.tell()
                    if pending_approvals > 0 and time.monotonic() - last_event > 120:
                        run_action_quiet("stop", env)
                        pending_approvals = 0
                    time.sleep(0.2)
                    continue

                position = handle.tell()
                if should_start_blink(line):
                    pending_approvals += 1
                    if pending_approvals == 1:
                        run_action_quiet("start", env)
                    last_event = time.monotonic()
                elif pending_approvals > 0 and should_stop_blink(line):
                    pending_approvals -= 1
                    if pending_approvals == 0:
                        run_action_quiet("stop", env)
                    last_event = time.monotonic()


def wait_for_log_position(stop_monitor: threading.Event) -> int:
    while not stop_monitor.is_set():
        if CODEX_LOG.exists():
            return CODEX_LOG.stat().st_size
        time.sleep(0.2)
    return 0


def should_start_blink(line: str) -> bool:
    if "ToolCall: exec_command" in line and '"sandbox_permissions":"require_escalated"' in line:
        return True
    if "ToolCall: apply_patch" in line:
        return True
    return False


def should_stop_blink(line: str) -> bool:
    return (
        'codex.op="exec_approval"' in line
        or 'codex.op="patch_approval"' in line
        or 'codex.op="interrupt"' in line
    )


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
