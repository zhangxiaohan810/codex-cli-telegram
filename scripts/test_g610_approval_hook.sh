#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

seconds="${1:-5}"

set -a
source .env
set +a

echo "start"
eval "$APPROVAL_REQUEST_START_CMD"

echo "sleep ${seconds}s"
sleep "$seconds"

echo "stop"
eval "$APPROVAL_REQUEST_STOP_CMD"
