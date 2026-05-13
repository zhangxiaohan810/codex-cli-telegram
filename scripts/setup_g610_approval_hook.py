#!/usr/bin/env python3
"""Interactive installer for the Logitech G610 approval hook."""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 19610
ENV_KEYS = {
    "G610_USAGE_MODE",
    "G610_LOCAL_OS",
    "G610_LOCAL_SERVER_PORT",
    "MAC_HOST",
    "MAC_SSH_USER",
    "MAC_G610_SERVER_PORT",
    "APPROVAL_REQUEST_START_CMD",
    "APPROVAL_REQUEST_STOP_CMD",
}


def run(args: list[str], *, input_text: str | None = None) -> None:
    subprocess.run(args, input=input_text, text=True, check=True)


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


def ask_text(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{suffix}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Choose y or n.")


def ssh_target(user: str, host: str) -> str:
    return f"{user}@{host}"


def ssh(user: str, host: str, command: str, *, input_text: str | None = None) -> None:
    run(["ssh", ssh_target(user, host), command], input_text=input_text)


def upload_text(user: str, host: str, remote_path: str, content: str) -> None:
    quoted = shlex.quote(remote_path)
    ssh(user, host, f"mkdir -p $(dirname {quoted}) && cat > {quoted} && chmod +x {quoted}", input_text=content)


def upload_home_bin(user: str, host: str, name: str, content: str) -> None:
    quoted_name = shlex.quote(name)
    ssh(user, host, f"mkdir -p ~/bin && cat > ~/bin/{quoted_name} && chmod +x ~/bin/{quoted_name}", input_text=content)


def update_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    keys = ENV_KEYS | set(values)
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in keys]
    kept.extend(f"{key}={value}" for key, value in values.items())
    path.write_text("\n".join(kept) + "\n")


def run_shell(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, shell=True, text=True, capture_output=True)


def install_mac_input_mapping_local() -> None:
    run(["python", str(ROOT / "scripts" / "install_mac_input_mapping.py")])


def install_mac_input_mapping_ssh(mac_user: str, mac_host: str, mac_dir: str) -> None:
    source = ROOT / "scripts" / "install_mac_input_mapping.py"
    remote = f"{mac_dir.rstrip('/')}/install_mac_input_mapping.py"
    upload_text(mac_user, mac_host, remote, source.read_text())
    ssh(mac_user, mac_host, f"python {shlex.quote(remote)}")


def run_blink_test(start_cmd: str, stop_cmd: str, seconds: int = 5) -> bool:
    print()
    print(f"Running {seconds}s G610 blink test...")
    start = run_shell(start_cmd)
    if start.stdout.strip():
        print(start.stdout.strip())
    if start.returncode != 0:
        if start.stderr.strip():
            print(start.stderr.strip())
        print("Blink test did not start.")
        return False

    time.sleep(seconds)

    stop = run_shell(stop_cmd)
    if stop.stdout.strip():
        print(stop.stdout.strip())
    if stop.returncode != 0 and stop.stderr.strip():
        print(stop.stderr.strip())
    print("Blink test completed.")
    return stop.returncode == 0


def make_mac_server_start_script(mac_python: str, server_remote: str) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

PY={shlex.quote(mac_python)}
SERVER={shlex.quote(server_remote)}
LOG="$HOME/.codex-g610-blink-server.log"
PIDFILE="$HOME/.codex-g610-blink-server.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running pid $(cat "$PIDFILE")"
  exit 0
fi

sudo -v
nohup sudo "$PY" "$SERVER" >"$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 0.5

if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  cat "$LOG" >&2
  rm -f "$PIDFILE"
  exit 1
fi

echo "started pid $(cat "$PIDFILE")"
"""


def make_mac_server_stop_script(port: int) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

PORT={port}
PIDFILE="$HOME/.codex-g610-blink-server.pid"

printf quit | nc 127.0.0.1 "$PORT" 2>/dev/null || true
sleep 0.2

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then
    sudo kill "$PID" 2>/dev/null || kill "$PID" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
fi

echo "stopped"
"""


def make_mac_server_status_script(port: int) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

PORT={port}
PIDFILE="$HOME/.codex-g610-blink-server.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  printf status | nc 127.0.0.1 "$PORT" || true
else
  echo "stopped"
fi
"""


def install_local_macos(env_file: Path, mac_python: str, port: int) -> tuple[str, str]:
    server_path = ROOT / "scripts" / "g610_blink_server.py"
    start_cmd = f"printf start | nc 127.0.0.1 {port}"
    stop_cmd = f"printf stop | nc 127.0.0.1 {port}"
    update_env(
        env_file,
        {
            "G610_USAGE_MODE": "local",
            "G610_LOCAL_OS": "mac",
            "G610_LOCAL_SERVER_PORT": str(port),
            "APPROVAL_REQUEST_START_CMD": f"\"{start_cmd}\"",
            "APPROVAL_REQUEST_STOP_CMD": f"\"{stop_cmd}\"",
        },
    )
    print(f"Updated {env_file}")
    print()
    print("Run this from a local Mac Terminal before starting Codex:")
    print(f"  sudo {mac_python} {server_path}")
    print()
    print("Then test:")
    print("  ./scripts/test_g610_approval_hook.sh 5")
    return start_cmd, stop_cmd


def install_ssh_macos(env_file: Path, mac_host: str, mac_user: str, mac_python: str, mac_dir: str, port: int) -> tuple[str, str]:
    server_source = ROOT / "scripts" / "g610_blink_server.py"
    server_remote = f"{mac_dir.rstrip('/')}/g610_blink_server.py"

    ssh(mac_user, mac_host, f"mkdir -p {shlex.quote(mac_dir)} ~/bin")
    upload_text(mac_user, mac_host, server_remote, server_source.read_text())
    upload_home_bin(mac_user, mac_host, "codex-g610-server-start", make_mac_server_start_script(mac_python, server_remote))
    upload_home_bin(mac_user, mac_host, "codex-g610-server-stop", make_mac_server_stop_script(port))
    upload_home_bin(mac_user, mac_host, "codex-g610-server-status", make_mac_server_status_script(port))

    start_cmd = f"ssh -o BatchMode=yes -o ConnectTimeout=5 {mac_user}@{mac_host} 'printf start | nc 127.0.0.1 {port}'"
    stop_cmd = f"ssh -o BatchMode=yes -o ConnectTimeout=5 {mac_user}@{mac_host} 'printf stop | nc 127.0.0.1 {port}'"
    update_env(
        env_file,
        {
            "G610_USAGE_MODE": "ssh",
            "G610_LOCAL_OS": "mac",
            "MAC_HOST": mac_host,
            "MAC_SSH_USER": mac_user,
            "MAC_G610_SERVER_PORT": str(port),
            "APPROVAL_REQUEST_START_CMD": f"\"{start_cmd}\"",
            "APPROVAL_REQUEST_STOP_CMD": f"\"{stop_cmd}\"",
        },
    )

    print("Installed Mac helper scripts:")
    print("  ~/bin/codex-g610-server-start")
    print("  ~/bin/codex-g610-server-stop")
    print("  ~/bin/codex-g610-server-status")
    print(f"Updated {env_file}")
    print()
    print("Run this once from a local Mac Terminal:")
    print("  ~/bin/codex-g610-server-start")
    print()
    print("Then test from this machine:")
    print("  ./scripts/test_g610_approval_hook.sh 5")
    return start_cmd, stop_cmd


def fail_windows() -> int:
    print("Windows G610 hook setup is not implemented yet.")
    print("This installer currently supports macOS because the verified HID path uses Python hidapi and a local Mac controller.")
    return 2


def default_mac_python() -> str:
    if platform.system() == "Darwin":
        candidate = Path.home() / "miniforge3" / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return "/Users/Tiezhu/miniforge3/bin/python"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["local", "ssh"], help="Where the bridge runs relative to the keyboard computer")
    parser.add_argument("--local-os", choices=["mac", "win"], help="OS of the computer that has the G610 plugged in")
    parser.add_argument("--mac-host", help="Mac IP, hostname, or Tailscale/MagicDNS name for ssh mode")
    parser.add_argument("--mac-user", default="Tiezhu", help="Mac SSH user")
    parser.add_argument("--mac-python", default=None, help="Python with hidapi on the Mac")
    parser.add_argument("--mac-dir", default="/Users/Tiezhu/Downloads/codex-g610", help="Install directory on the Mac")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Local controller TCP port")
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="Bridge .env path")
    parser.add_argument("--test", action="store_true", help="Run a 5-second blink test after setup")
    parser.add_argument("--no-test", action="store_true", help="Skip the post-setup blink test prompt")
    parser.add_argument("--input-mapping", choices=["ask", "yes", "no"], default="ask", help="Install macOS keyboard/mouse mapping templates")
    args = parser.parse_args()

    mode = args.mode or ask_choice("Use the G610 on this machine or over SSH?", ["local", "ssh"], "ssh")
    local_os = args.local_os or ask_choice("Which OS has the G610 plugged in?", ["mac", "win"], "mac")
    env_file = Path(args.env_file)

    if local_os == "win":
        return fail_windows()

    mac_python = args.mac_python or ask_text("Python path on the Mac with hidapi", default_mac_python())

    if mode == "local":
        start_cmd, stop_cmd = install_local_macos(env_file, mac_python, args.port)
        if args.input_mapping == "yes" or (args.input_mapping == "ask" and ask_yes_no("Install optional Mac keyboard/mouse mapping templates?", False)):
            install_mac_input_mapping_local()
        should_test = args.test or (not args.no_test and ask_yes_no("Run 5-second blink test now?", True))
        if should_test:
            run_blink_test(start_cmd, stop_cmd)
        return 0

    mac_host = args.mac_host or ask_text("Mac host/IP")
    mac_user = args.mac_user or ask_text("Mac SSH user", os.environ.get("USER", "Tiezhu"))
    mac_dir = args.mac_dir or ask_text("Install directory on Mac", "/Users/Tiezhu/Downloads/codex-g610")
    start_cmd, stop_cmd = install_ssh_macos(env_file, mac_host, mac_user, mac_python, mac_dir, args.port)
    if args.input_mapping == "yes" or (args.input_mapping == "ask" and ask_yes_no("Install optional Mac keyboard/mouse mapping templates on the Mac?", False)):
        install_mac_input_mapping_ssh(mac_user, mac_host, mac_dir)
    should_test = args.test or (not args.no_test and ask_yes_no("Run 5-second blink test now?", True))
    if should_test:
        ok = run_blink_test(start_cmd, stop_cmd)
        if not ok:
            print()
            print("If this is an SSH-to-Mac setup, start the Mac-side controller from a local Mac Terminal first:")
            print("  ~/bin/codex-g610-server-start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
