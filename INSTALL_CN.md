# Codex Telegram Bridge 安装和使用

这份文档用于在另一台电脑或服务器上安装 `codex-cli-telegram`，让 Codex CLI 的输出和审批请求同步到 Telegram。

## 1. 从 Git 下载并安装

先确认机器上已经安装 Node.js 和 Codex CLI：

```bash
node --version
python3 --version
codex --version
```

建议 Node.js 版本为 22 或更高。Python 3 用于 PTY driver。然后下载项目并安装命令：

```bash
git clone https://github.com/zhangxiaohan810/codex-cli-telegram.git
cd codex-cli-telegram
npm install -g .
```

安装完成后，系统里会有两个命令：

```bash
codex-telegram
codex-telegram-bridge
```

## 2. 用 BotFather 新建 Telegram 机器人

打开 Telegram，搜索并进入官方机器人：

```text
@BotFather
```

发送：

```text
/newbot
```

按提示设置机器人名称和用户名。创建成功后，BotFather 会返回一个 token，格式类似：

```text
1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

这个值就是后面 `.env` 里的 `TELEGRAM_BOT_TOKEN`。

创建完成后，打开你新建的 bot，先给它发送任意一条消息，例如：

```text
hello
```

## 3. 配置环境参数

在项目目录创建 `.env`：

```bash
cd codex-cli-telegram
cp .env.example .env
nano .env
```

至少需要修改这几项：

```bash
TELEGRAM_BOT_TOKEN=你的_BotFather_token
TELEGRAM_CHAT_ID=你的_数字_chat_id
TELEGRAM_PROXY=http://127.0.0.1:8888
```

### 获取 TELEGRAM_CHAT_ID

先确认你已经给新建的 bot 发过消息，然后在浏览器打开：

```text
https://api.telegram.org/bot<你的_BotFather_token>/getUpdates
```

注意：URL 里 token 前面必须加 `bot`。

例如 token 是：

```text
1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

那么网页地址应该是：

```text
https://api.telegram.org/bot1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/getUpdates
```

打开后，在返回内容里找：

```json
"chat":{"id":123456789}
```

这里的数字 `123456789` 就是：

```bash
TELEGRAM_CHAT_ID=123456789
```

### 配置 TELEGRAM_PROXY

如果服务器或电脑访问 Telegram API 需要 VPN/代理，就配置 `TELEGRAM_PROXY`。

例如 Clash HTTP 代理端口是 `8888`：

```bash
TELEGRAM_PROXY=http://127.0.0.1:8888
```

如果这台机器不需要代理，或者没有运行 Clash，就把这一行注释掉：

```bash
# TELEGRAM_PROXY=http://127.0.0.1:8888
```

常用 `.env` 示例：

```bash
TELEGRAM_BOT_TOKEN=1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
CODEX_UPSTREAM_WS=ws://127.0.0.1:8765
BRIDGE_HOST=127.0.0.1
BRIDGE_PORT=8766
PTY_CONTROL_HOST=127.0.0.1
PTY_CONTROL_PORT=8767
TELEGRAM_INPUT_MODE=tui
MIRROR_AGENT_MESSAGES=1
MIRROR_PROCESS_EVENTS=0
INCLUDE_APPROVAL_PARAMS=0
BRIDGE_DEBUG_RPC=0
TELEGRAM_RETRIES=2
TELEGRAM_SAFE_MESSAGE_LIMIT=3800
TELEGRAM_PROXY=http://127.0.0.1:8888
```

## 4. 启动和使用

配置好后，在项目目录运行：

```bash
codex-telegram
```

它会自动做三件事：

1. 启动 `codex app-server`，默认地址是 `ws://127.0.0.1:8765`
2. 启动 `codex-telegram-bridge`，默认地址是 `ws://127.0.0.1:8766`
3. 通过 PTY driver 打开 `codex --remote ws://127.0.0.1:8766`

只有 `.env` 里配置了 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 时，`codex-telegram` 才会启动 Telegram 通知。`codex-keyboard` 不启动 Telegram，但会为当前窗口自动分配一组本地空闲端口来启动 `codex app-server` 和 keyboard bridge，然后打开 `codex --remote`，这样键盘闪烁绑定的是 bridge 收到的真实 `requestApproval` 事件。

启动成功后，你可以正常在 Codex CLI 里使用 Codex。Telegram bot 会收到 Codex 的输出和审批按钮。

如果当前不需要 Telegram，只想在 Codex 审批时通过 SSH 让 Mac 上的 G610 闪烁，可以直接运行下面的入口：

```bash
codex-keyboard
codex-keyboard status
codex-keyboard test 5
```

手动控制键盘闪烁时再用：

```bash
codex-keyboard start
codex-keyboard stop
```

### 推荐的 Mac 键盘反向转发方式

Mac 控制键盘时，推荐让键盘命令监听器运行在 Mac 本地，再通过 SSH `RemoteForward` 暴露到每台服务器。这样服务器不需要知道 Mac 的局域网 IP。

1. 在 Mac 本地启动端口命令监听器。可以直接运行 `~/bin/codex-g610-server-start`，也可以使用 [Local Keyboard Notifier](https://github.com/zhangxiaohan810/claude-codex-keyboard-notifier)，确保 Mac 本地 `127.0.0.1:19610` 可用。
2. 在 Mac 端连接服务器的 SSH config 里加远程转发：

```sshconfig
Host Server
    HostName server.example.com
    User your_server_user
    RemoteForward 19610 127.0.0.1:19610
```

`19610` 可以换成任何你想要的服务器端空闲端口；右侧保持为 Mac 本地监听器端口，通常是 `127.0.0.1:19610`。

3. 服务器端 `.env` 里把审批 hook 指向本地反向转发端口：

```bash
APPROVAL_REQUEST_START_CMD="printf start | nc 127.0.0.1 19610"
APPROVAL_REQUEST_STOP_CMD="printf stop | nc 127.0.0.1 19610"
APPROVAL_REQUEST_STATUS_CMD="printf status | nc 127.0.0.1 19610"
```

服务器上验证：

```bash
printf status | nc 127.0.0.1 19610
codex-keyboard status
```

如果 `codex-keyboard` 找不到 Codex，或者误用 `/snap/bin/codex` 这类旧路径，在 `.env` 里显式指定：

```bash
CODEX_BIN=/home/you/.nvm/versions/node/<version>/bin/codex
```

### Telegram 命令同步说明

Telegram 发来的普通文字和 Codex slash 命令都会写入屏幕里的 Codex CLI，所以它等价于你在终端里输入。

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

`/stop` 保留为 bridge 层的中断命令，用来打断当前 Codex turn。`/bridge_status` 用来看 bridge 连接状态。其它 `/` 命令会通过 PTY driver 输入到屏幕 Codex CLI，不再创建 Telegram-only 状态。

如果你在服务器 SSH 里使用，建议用 `tmux` 防止断连：

```bash
tmux new -s codex-telegram
cd /path/to/codex-cli-telegram
codex-telegram
```

断开 tmux：

```text
Ctrl-b d
```

重新进入：

```bash
tmux attach -t codex-telegram
```

## 常见问题

如果 Telegram 没反应，先看日志：

```bash
tail -f .logs/codex-telegram-bridge.log
```

如果提示没有 active CLI，说明 `codex --remote ws://127.0.0.1:8766` 没连上 bridge，重新运行：

```bash
codex-telegram
```

同一个 Telegram bot 不建议同时在两台电脑上运行 bridge，否则消息可能被另一台机器抢走。多台机器同时用时，建议每台机器单独创建一个 bot。

同一台机器上只保留一个 `codex-telegram` 主窗口。Telegram 输入只绑定第一个屏幕 `codex --remote` 连接；Codex TUI 内部为了 `/resume` 等功能额外创建的连接会作为辅助连接透传，不会接管 Telegram 路由。
