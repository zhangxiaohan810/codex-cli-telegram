# Codex Telegram App Bridge

This is a small local bridge for Codex app-server. It lets your normal Codex screen client and Telegram approve the same Codex permission prompt. Whichever side answers first wins; duplicate approval responses are ignored by the bridge.

## What It Does

- Proxies `codex --remote` traffic to a real `codex app-server`.
- Mirrors Codex approval requests to Telegram with inline buttons.
- Lets Telegram answer command and file-change approvals with `accept`, `acceptForSession`, `decline`, or `cancel`.
- Mirrors assistant text deltas to Telegram in small batches.

This bridge uses Codex's local app-server JSON-RPC protocol. It does not scrape terminal text or simulate keyboard input.

## Install On A Server

Install prerequisites:

```bash
node --version
codex --version
```

Node 22+ is recommended. Codex must support `app-server` and `--remote`.

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
CODEX_UPSTREAM_WS=ws://127.0.0.1:8765
BRIDGE_HOST=127.0.0.1
BRIDGE_PORT=8766
# If Telegram is blocked on your network, use your local proxy:
TELEGRAM_PROXY=http://127.0.0.1:8888
```

Get your Telegram `chat_id` by sending a message to your bot, then opening:

```text
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Look for `message.chat.id`.

## Run

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
codex --remote ws://127.0.0.1:8766
```

Now use Codex normally on screen. When Codex asks for approval, Telegram receives buttons too.

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

## Notes

- Keep both WebSocket listeners on `127.0.0.1` unless you have a separate authenticated tunnel.
- The bridge only accepts callback clicks from `TELEGRAM_CHAT_ID`.
- `item/permissions/requestApproval` is mirrored for visibility, but the first version does not synthesize a permission profile response from Telegram because that response shape is more complex than command/file-change approvals. Approve those on screen.
