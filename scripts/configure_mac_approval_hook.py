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
    "APPROVAL_REQUEST_STATUS_CMD",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mac_host", nargs="?", help="Mac IP, hostname, or Tailscale/MagicDNS name")
    parser.add_argument("--user", default="Tiezhu", help="Mac SSH user")
    parser.add_argument("--port", type=int, default=19610, help="Mac-local G610 server port")
    parser.add_argument("--remote-forward", action="store_true", help="Use an SSH RemoteForward exposed on this server instead of SSHing to the Mac")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    env_path = Path(args.env_file)
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in KEYS]

    if args.remote_forward:
        kept.extend(
            [
                "MAC_HOST=127.0.0.1",
                f"MAC_G610_SERVER_PORT={args.port}",
                "G610_USAGE_MODE=remote-forward",
                "G610_LOCAL_OS=mac",
                f"APPROVAL_REQUEST_START_CMD=\"printf start | nc 127.0.0.1 {args.port}\"",
                f"APPROVAL_REQUEST_STOP_CMD=\"printf stop | nc 127.0.0.1 {args.port}\"",
                f"APPROVAL_REQUEST_STATUS_CMD=\"printf status | nc 127.0.0.1 {args.port}\"",
            ]
        )
        description = f"server-local RemoteForward port 127.0.0.1:{args.port}"
    else:
        if not args.mac_host:
            raise SystemExit("mac_host is required unless --remote-forward is used")
        kept.extend(
            [
                f"MAC_HOST={args.mac_host}",
                f"MAC_SSH_USER={args.user}",
                f"MAC_G610_SERVER_PORT={args.port}",
                "G610_USAGE_MODE=ssh",
                "G610_LOCAL_OS=mac",
                f"APPROVAL_REQUEST_START_CMD=\"ssh -o BatchMode=yes -o ConnectTimeout=5 $MAC_SSH_USER@$MAC_HOST 'printf start | nc 127.0.0.1 {args.port}'\"",
                f"APPROVAL_REQUEST_STOP_CMD=\"ssh -o BatchMode=yes -o ConnectTimeout=5 $MAC_SSH_USER@$MAC_HOST 'printf stop | nc 127.0.0.1 {args.port}'\"",
                f"APPROVAL_REQUEST_STATUS_CMD=\"ssh -o BatchMode=yes -o ConnectTimeout=5 $MAC_SSH_USER@$MAC_HOST 'printf status | nc 127.0.0.1 {args.port}'\"",
            ]
        )
        description = f"{args.user}@{args.mac_host}"

    env_path.write_text("\n".join(kept) + "\n")
    print(f"Updated {env_path} for {description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
