#!/usr/bin/env python3
"""Update .env so approval hooks SSH to the Mac that controls the G610."""

from __future__ import annotations

import argparse
from pathlib import Path


KEYS = {
    "MAC_HOST",
    "MAC_SSH_USER",
    "MAC_G610_SERVER_PORT",
    "G610_USAGE_MODE",
    "G610_LOCAL_OS",
    "APPROVAL_REQUEST_START_CMD",
    "APPROVAL_REQUEST_STOP_CMD",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mac_host", help="Mac IP, hostname, or Tailscale/MagicDNS name")
    parser.add_argument("--user", default="Tiezhu", help="Mac SSH user")
    parser.add_argument("--port", type=int, default=19610, help="Mac-local G610 server port")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    env_path = Path(args.env_file)
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in KEYS]

    kept.extend(
        [
            f"MAC_HOST={args.mac_host}",
            f"MAC_SSH_USER={args.user}",
            f"MAC_G610_SERVER_PORT={args.port}",
            "G610_USAGE_MODE=ssh",
            "G610_LOCAL_OS=mac",
            f"APPROVAL_REQUEST_START_CMD=ssh -o BatchMode=yes -o ConnectTimeout=5 $MAC_SSH_USER@$MAC_HOST 'printf start | nc 127.0.0.1 {args.port}'",
            f"APPROVAL_REQUEST_STOP_CMD=ssh -o BatchMode=yes -o ConnectTimeout=5 $MAC_SSH_USER@$MAC_HOST 'printf stop | nc 127.0.0.1 {args.port}'",
        ]
    )

    env_path.write_text("\n".join(kept) + "\n")
    print(f"Updated {env_path} for {args.user}@{args.mac_host}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
