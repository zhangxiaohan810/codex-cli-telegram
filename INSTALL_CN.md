# Codex Telegram Bridge 安装和使用

这份文档用于在另一台电脑或服务器上安装 `codex-cli-telegram`，让 Codex CLI 的输出和审批请求同步到 Telegram。

## 1. 从 Git 下载并安装

先确认机器上已经安装 Node.js 和 Codex CLI：

```bash
node --version
codex --version
```

建议 Node.js 版本为 22 或更高。然后下载项目并安装命令：

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
3. 打开 `codex --remote ws://127.0.0.1:8766`

启动成功后，你可以正常在 Codex CLI 里使用 Codex。Telegram bot 会收到 Codex 的输出和审批按钮。

### Telegram 命令同步说明

Telegram 可以直接发送普通文字给当前屏幕里的 Codex session，也可以处理这些同步命令：

```text
/status
/diff
/review
/compact
/stop
```

下面这些命令属于 Codex CLI 屏幕本地状态，必须在终端里的 Codex CLI 输入：

```text
/model
/reasoning
/approvals
/new
/resume
```

原因是 Codex CLI 0.130.0 还没有提供“让 bridge 远程执行 TUI 里的 slash 命令并切换本地状态”的协议。如果 bridge 在 Telegram 里私自改模型、推理强度、权限模式或 session，会造成 Telegram 和终端屏幕看到的状态不同步，所以新版会拒绝这种 Telegram-only 修改。

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

同一台机器上也只保留一个 `codex-telegram` 或 `codex --remote ws://127.0.0.1:8766` 窗口。新版 bridge 会拒绝第二个 CLI 连接，防止 Telegram 输入串到错误窗口。
