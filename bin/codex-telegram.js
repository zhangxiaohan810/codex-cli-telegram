#!/usr/bin/env node

import { spawn } from "node:child_process";
import { createConnection } from "node:net";
import { existsSync, mkdirSync, openSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const env = loadEnv(projectRoot);
const logsDir = resolve(projectRoot, ".logs");
const codexBin = env.CODEX_BIN || defaultCodexBin();
const upstreamUrl = new URL(env.CODEX_UPSTREAM_WS || "ws://127.0.0.1:8765");
const bridgeHost = env.BRIDGE_HOST || "127.0.0.1";
const bridgePort = Number(env.BRIDGE_PORT || 8766);
const bridgeUrl = `ws://${bridgeHost}:${bridgePort}`;
const ptyControlHost = env.PTY_CONTROL_HOST || "127.0.0.1";
const ptyControlPort = Number(env.PTY_CONTROL_PORT || 8767);
const pythonBin = env.PYTHON_BIN || "python3";

mkdirSync(logsDir, { recursive: true });

await ensureListening({
  name: "codex app-server",
  host: upstreamUrl.hostname,
  port: Number(upstreamUrl.port || 80),
  command: codexBin,
  args: ["app-server", "--listen", env.CODEX_UPSTREAM_WS || "ws://127.0.0.1:8765"],
  log: resolve(logsDir, "codex-app-server.log"),
});

await ensureListening({
  name: "codex-telegram-bridge",
  host: bridgeHost,
  port: bridgePort,
  command: "codex-telegram-bridge",
  args: [],
  cwd: projectRoot,
  log: resolve(logsDir, "codex-telegram-bridge.log"),
});

console.log(`[codex-telegram] starting Codex CLI: ${codexBin} --remote ${bridgeUrl}`);
const cli = spawn(pythonBin, [
  resolve(projectRoot, "scripts/codex_pty_driver.py"),
  "--control-host",
  ptyControlHost,
  "--control-port",
  String(ptyControlPort),
  codexBin,
  "--remote",
  bridgeUrl,
  ...process.argv.slice(2),
], {
  stdio: "inherit",
  env: {
    ...env,
    PTY_CONTROL_HOST: ptyControlHost,
    PTY_CONTROL_PORT: String(ptyControlPort),
  },
});

cli.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 0);
});

async function ensureListening({ name, host, port, command, args, cwd = projectRoot, log }) {
  if (await canConnect(host, port)) {
    console.log(`[codex-telegram] ${name} already listening on ${host}:${port}`);
    return;
  }

  console.log(`[codex-telegram] starting ${name} on ${host}:${port}`);
  const fd = openSync(log, "a");
  const child = spawn(command, args, {
    cwd,
    detached: true,
    stdio: ["ignore", fd, fd],
    env,
  });
  child.unref();

  const deadline = Date.now() + 12_000;
  while (Date.now() < deadline) {
    if (await canConnect(host, port)) {
      console.log(`[codex-telegram] ${name} ready; log: ${log}`);
      return;
    }
    await sleep(300);
  }

  throw new Error(`${name} did not start on ${host}:${port}. Check log: ${log}`);
}

function canConnect(host, port) {
  return new Promise((resolveConnect) => {
    const socket = createConnection({ host, port, timeout: 700 });
    socket.once("connect", () => {
      socket.destroy();
      resolveConnect(true);
    });
    socket.once("timeout", () => {
      socket.destroy();
      resolveConnect(false);
    });
    socket.once("error", () => resolveConnect(false));
  });
}

function sleep(ms) {
  return new Promise((resolveSleep) => setTimeout(resolveSleep, ms));
}

function defaultCodexBin() {
  const macAppBin = "/Applications/Codex.app/Contents/Resources/codex";
  return existsSync(macAppBin) ? macAppBin : "codex";
}

function loadEnv(projectRoot) {
  const output = { ...process.env };
  const cwdPath = resolve(process.cwd(), ".env");
  const projectPath = resolve(projectRoot, ".env");
  const path = existsSync(cwdPath) ? cwdPath : projectPath;
  if (!existsSync(path)) return output;

  const content = readFileSync(path, "utf8");
  for (const line of content.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const index = trimmed.indexOf("=");
    if (index === -1) continue;
    const key = trimmed.slice(0, index).trim();
    let value = trimmed.slice(index + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (!(key in output)) output[key] = value;
  }
  return output;
}
