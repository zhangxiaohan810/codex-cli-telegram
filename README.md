# Codex Telegram App Bridge

This is a small local bridge for Codex app-server. It lets your normal Codex screen client and Telegram approve the same Codex permission prompt. Whichever side answers first wins; duplicate approval responses are ignored by the bridge.

Chinese installation guide: [INSTALL_CN.md](./INSTALL_CN.md)

## What It Does

- Proxies `codex --remote` traffic to a real `codex app-server`.
- Mirrors Codex approval requests to Telegram with inline buttons.
- Lets Telegram answer command and file-change approvals with `accept`, `acceptForSession`, `decline`, or `cancel`.
- Mirrors assistant text deltas to Telegram in small batches.
- Registers Telegram slash-command suggestions. Synced commands are handled by the bridge so they are not sent as normal prompt text.

This bridge uses Codex's local app-server JSON-RPC protocol. It does not scrape terminal text or simulate keyboard input.

## Install On A Server

Install prerequisites:

```bash
node --version
python3 --version
codex --version
```

Node 22+ is recommended. Python 3 is used by the PTY driver. Codex must support `app-server` and `--remote`.

Clone or copy this folder to the server, then install the CLI link:

```bash
cd /path/to/codex-cli-telegram
npm install -g .
```

Create config:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
TELEGRAM_BOT_TOKEN=your_botfather_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
# Approval notification channel: telegram, keyboard, or both.
NOTIFICATION_CHANNEL=telegram
# Optional on macOS if `codex` is not in PATH:
# CODEX_BIN=/Applications/Codex.app/Contents/Resources/codex
CODEX_UPSTREAM_WS=ws://127.0.0.1:8765
BRIDGE_HOST=127.0.0.1
BRIDGE_PORT=8766
PTY_CONTROL_HOST=127.0.0.1
PTY_CONTROL_PORT=8767
TELEGRAM_INPUT_MODE=tui
MIRROR_AGENT_MESSAGES=1
MIRROR_PROCESS_EVENTS=0
INCLUDE_APPROVAL_PARAMS=0
# Optional: log bridge JSON-RPC requests/responses while debugging commands:
BRIDGE_DEBUG_RPC=0
# Optional: run a local command while a Codex approval request is pending:
APPROVAL_REQUEST_START_CMD=
APPROVAL_REQUEST_STOP_CMD=
# Optional: retry transient Telegram API network failures such as ECONNRESET:
TELEGRAM_RETRIES=2
# Optional: conservative per-message HTML size budget for Telegram splitting:
TELEGRAM_SAFE_MESSAGE_LIMIT=3800
# If Telegram is blocked on your network, use your local proxy:
TELEGRAM_PROXY=http://127.0.0.1:8888
```

Get your Telegram `chat_id` by sending a message to your bot, then opening:

```text
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Look for `message.chat.id`.

## Run

Recommended one-command startup:

```bash
cd /path/to/codex-cli-telegram
codex-telegram
```

This starts `codex app-server` and `codex-telegram-bridge` in the background if they are not already listening, then opens:

```bash
codex --remote ws://127.0.0.1:8766
```

The visible Codex CLI is wrapped by a small Python PTY driver. Telegram input is typed into that same screen CLI through `127.0.0.1:8767`, so slash commands like `/model`, `/new`, and `/resume` run through Codex's native TUI command handling instead of changing bridge-only state.

Logs are written under `.logs/`.

Manual startup is still available:

Terminal 1:

```bash
codex app-server --listen ws://127.0.0.1:8765
```

Terminal 2:

```bash
cd /path/to/codex-cli-telegram
codex-telegram-bridge
```

Terminal 3:

```bash
python3 scripts/codex_pty_driver.py --control-host 127.0.0.1 --control-port 8767 codex --remote ws://127.0.0.1:8766
```

Now use Codex normally on screen. When Codex asks for approval, Telegram receives buttons too.

## Approval Request Hook

Run the notification installer to choose how approval requests reach you:

```bash
codex-notify-setup
```

It asks whether the communication method is Telegram, keyboard light, or both. Telegram writes `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; keyboard light delegates to `codex-g610-setup`.

Set `APPROVAL_REQUEST_START_CMD` to run a local command while any Codex approval request is pending. The bridge starts it when the first approval arrives and stops it when all pending approvals are resolved. `APPROVAL_REQUEST_STOP_CMD` is optional and runs after the start process is stopped.

For a Logitech G610, run the interactive installer:

```bash
codex-g610-setup
```

It asks whether the bridge runs locally or over SSH, then asks whether the computer with the G610 is macOS or Windows. The currently implemented path is macOS. Windows is detected but not configured yet.
After setup, it offers a 5-second blink test. You can force or skip that prompt with `--test` or `--no-test`.
On macOS it also asks whether to install optional keyboard/mouse mapping templates. The default keyboard template maps external G610 `Ctrl+C/V/B/Z/S/...` to `Command+C/V/B/Z/S/...` through Karabiner-Elements. The mouse template documents per-device reverse wheel scrolling through LinearMouse, so the trackpad is not changed.

For the verified server-to-Mac setup:

```bash
python scripts/setup_g610_approval_hook.py --mode ssh --local-os mac --mac-host 10.7.166.81 --mac-user Tiezhu
ssh-copy-id Tiezhu@10.7.166.81
```

Then start the Mac-side controller from a local Mac Terminal with `~/bin/codex-g610-server-start`, and test from the bridge machine with `codex-g610-test 5`.

## Run On A Headless Server

For SSH-only use, keep the services in `tmux`:

```bash
tmux new -s codex-app
codex app-server --listen ws://127.0.0.1:8765
```

Detach with `Ctrl-b d`, then:

```bash
tmux new -s codex-bridge
cd /path/to/codex-cli-telegram
codex-telegram-bridge
```

In your normal SSH shell:

```bash
codex --remote ws://127.0.0.1:8766
```

If you prefer systemd, create `~/.config/systemd/user/codex-app-server.service`:

```ini
[Unit]
Description=Codex app-server

[Service]
ExecStart=/usr/bin/env codex app-server --listen ws://127.0.0.1:8765
Restart=on-failure

[Install]
WantedBy=default.target
```

Create `~/.config/systemd/user/codex-telegram-bridge.service`:

```ini
[Unit]
Description=Codex Telegram bridge
After=codex-app-server.service

[Service]
WorkingDirectory=/path/to/codex-cli-telegram
ExecStart=/usr/bin/env codex-telegram-bridge
Restart=on-failure

[Install]
WantedBy=default.target
```

Enable them:

```bash
systemctl --user daemon-reload
systemctl --user enable --now codex-app-server codex-telegram-bridge
```

## Telegram Buttons

- `Accept`: approve this request.
- `Session`: approve for the current session when Codex supports it.
- `Decline`: reject and let the agent continue.
- `Cancel`: reject and interrupt the turn.

## Telegram Commands

Typing `/` in the Telegram chat shows a command menu.

Bridge-local commands:

```text
/bridge_help
/bridge_status
```

Codex slash commands are typed into the screen CLI:

```text
/model
/reasoning
/permissions
/status
/diff
/review
/compact
/new
/resume
```

`/stop` is kept as a bridge-level interrupt for the active Codex turn. `/bridge_status` shows bridge diagnostics. Other slash commands are passed to the screen CLI instead of being sent as normal prompt text.

For debugging command routing, set `BRIDGE_DEBUG_RPC=1` in `.env` and restart `codex-telegram-bridge`.

## Notes

- Keep both WebSocket listeners on `127.0.0.1` unless you have a separate authenticated tunnel.
- Telegram input is bound to the first active screen `codex --remote` client. Extra WebSocket connections are proxied as auxiliary app-server connections so Codex TUI features such as `/resume` can load session data, but they do not take over Telegram routing.
- A Telegram bot token can only be polled by one bridge process at a time. If logs show `getUpdates: Conflict`, stop the other bridge or use a separate bot token for that machine.
- The bridge only accepts callback clicks from `TELEGRAM_CHAT_ID`.
- `item/permissions/requestApproval` is mirrored for visibility, but the first version does not synthesize a permission profile response from Telegram because that response shape is more complex than command/file-change approvals. Approve those on screen.
