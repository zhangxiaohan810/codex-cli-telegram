#!/usr/bin/env node

import { createHash, randomBytes } from "node:crypto";
import { createServer } from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

const env = loadEnv();

const TELEGRAM_BOT_TOKEN = env.TELEGRAM_BOT_TOKEN;
const TELEGRAM_CHAT_ID = env.TELEGRAM_CHAT_ID;
const CODEX_UPSTREAM_WS = env.CODEX_UPSTREAM_WS || "ws://127.0.0.1:8765";
const BRIDGE_HOST = env.BRIDGE_HOST || "127.0.0.1";
const BRIDGE_PORT = Number(env.BRIDGE_PORT || 8766);
const MIRROR_AGENT_MESSAGES = env.MIRROR_AGENT_MESSAGES !== "0";

if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
  fatal("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required. Copy .env.example to .env first.");
}

const upstreamByClient = new Map();
const pendingApprovals = new Map();
const seenClientResponses = new Set();
const agentBuffers = new Map();
let telegramOffset = 0;

startTelegramPolling().catch((error) => {
  console.error("[telegram] polling stopped:", error);
});

const server = createServer();
server.on("upgrade", (req, socket, head) => {
  if (req.headers.upgrade?.toLowerCase() !== "websocket") {
    socket.destroy();
    return;
  }

  const client = acceptWebSocket(req, socket, head);
  const upstream = new WebSocket(CODEX_UPSTREAM_WS);
  upstreamByClient.set(client, upstream);

  console.log(`[bridge] client connected, upstream=${CODEX_UPSTREAM_WS}`);

  client.onMessage = (data) => handleClientMessage(client, upstream, data);
  upstream.addEventListener("message", (event) => handleUpstreamMessage(client, upstream, event.data));

  client.onClose = () => closePair(client, upstream);
  client.onError = (error) => {
    console.error("[bridge] client error:", error.message);
    closePair(client, upstream);
  };

  upstream.addEventListener("open", () => {
    client.flushQueue?.();
  });
  upstream.addEventListener("close", () => closePair(client, upstream));
  upstream.addEventListener("error", (error) => {
    console.error("[bridge] upstream error:", error.message);
    closePair(client, upstream);
  });
});

server.listen(BRIDGE_PORT, BRIDGE_HOST, () => {
  console.log(`[bridge] listening ws://${BRIDGE_HOST}:${BRIDGE_PORT}`);
  console.log(`[bridge] connect screen client with: codex --remote ws://${BRIDGE_HOST}:${BRIDGE_PORT}`);
});

function handleClientMessage(client, upstream, data) {
  const text = data.toString();
  const msg = parseJson(text);

  if (msg?.id !== undefined && seenClientResponses.has(requestKey(client, msg.id))) {
    console.log(`[bridge] swallowed duplicate response id=${msg.id}`);
    return;
  }

  sendUpstream(upstream, text);
}

function handleUpstreamMessage(client, upstream, data) {
  const text = data.toString();
  const msg = parseJson(text);

  if (msg) {
    if (isApprovalRequest(msg)) {
      registerApproval(client, upstream, msg);
    } else if (msg.method === "serverRequest/resolved") {
      markResolved(client, msg.params?.requestId, "screen");
    } else {
      mirrorNotification(msg);
    }
  }

  client.sendText(text);
}

function isApprovalRequest(msg) {
  return msg?.id !== undefined && [
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
  ].includes(msg.method);
}

function registerApproval(client, upstream, msg) {
  const key = requestKey(client, msg.id);
  if (pendingApprovals.has(key)) return;

  const record = {
    key,
    client,
    upstream,
    requestId: msg.id,
    method: msg.method,
    params: msg.params || {},
    telegramMessageId: null,
    resolved: false,
  };

  pendingApprovals.set(key, record);
  sendApprovalToTelegram(record).catch((error) => {
    console.error("[telegram] failed to send approval:", error.message);
  });
}

async function sendApprovalToTelegram(record) {
  const text = formatApproval(record);
  const supportsSimpleDecision = record.method !== "item/permissions/requestApproval";
  const callbackPrefix = shortApprovalToken(record.key);

  const buttons = supportsSimpleDecision
    ? [
        [
          { text: "Accept", callback_data: `a:${callbackPrefix}:accept` },
          { text: "Session", callback_data: `a:${callbackPrefix}:acceptForSession` },
        ],
        [
          { text: "Decline", callback_data: `a:${callbackPrefix}:decline` },
          { text: "Cancel", callback_data: `a:${callbackPrefix}:cancel` },
        ],
      ]
    : [[{ text: "Approve on screen", callback_data: `noop:${callbackPrefix}` }]];

  record.callbackPrefix = callbackPrefix;

  const result = await telegram("sendMessage", {
    chat_id: TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
    reply_markup: { inline_keyboard: buttons },
    disable_web_page_preview: true,
  });

  record.telegramMessageId = result.message_id;
}

function formatApproval(record) {
  const p = record.params;
  if (record.method === "item/commandExecution/requestApproval") {
    return html([
      "<b>Codex command approval</b>",
      "",
      `cwd: <code>${p.cwd || ""}</code>`,
      `cmd: <code>${p.command || "(command unavailable)"}</code>`,
      p.reason ? `reason: ${p.reason}` : "",
      p.proposedExecpolicyAmendment ? `policy: <code>${JSON.stringify(p.proposedExecpolicyAmendment)}</code>` : "",
    ].filter(Boolean).join("\n"));
  }

  if (record.method === "item/fileChange/requestApproval") {
    return html([
      "<b>Codex file-change approval</b>",
      "",
      `thread: <code>${p.threadId || ""}</code>`,
      `turn: <code>${p.turnId || ""}</code>`,
      `item: <code>${p.itemId || ""}</code>`,
    ].join("\n"));
  }

  return html([
    "<b>Codex permissions approval</b>",
    "",
    "Telegram mirror is read-only for this request type. Approve or deny it on the screen client.",
    "",
    `cwd: <code>${p.cwd || ""}</code>`,
    p.reason ? `reason: ${p.reason}` : "",
    `permissions: <code>${JSON.stringify(p.permissions || {})}</code>`,
  ].filter(Boolean).join("\n"));
}

async function handleTelegramCallback(query) {
  const data = query.data || "";
  await telegram("answerCallbackQuery", { callback_query_id: query.id });

  if (String(query.from?.id) !== String(TELEGRAM_CHAT_ID)) return;
  if (data.startsWith("noop:")) return;

  const parts = data.split(":");
  if (parts.length !== 3 || parts[0] !== "a") return;

  const [, token, decision] = parts;
  const record = [...pendingApprovals.values()].find((candidate) => candidate.callbackPrefix === token);
  if (!record || record.resolved) {
    await editTelegramApproval(query.message, "Already resolved.");
    return;
  }

  const response = {
    jsonrpc: "2.0",
    id: record.requestId,
    result: { decision },
  };

  sendUpstream(record.upstream, JSON.stringify(response));
  markResolved(record.client, record.requestId, `telegram:${decision}`);
  await editTelegramApproval(query.message, `Resolved from Telegram: ${decision}`);
}

function markResolved(client, requestId, source) {
  if (requestId === undefined || requestId === null) return;

  const key = requestKey(client, requestId);
  const record = pendingApprovals.get(key);
  if (!record) return;

  record.resolved = true;
  seenClientResponses.add(key);
  pendingApprovals.delete(key);

  if (record.telegramMessageId && !source.startsWith("telegram:")) {
    editTelegramApproval({ chat: { id: TELEGRAM_CHAT_ID }, message_id: record.telegramMessageId }, `Resolved from ${source}.`).catch(() => {});
  }
}

async function editTelegramApproval(message, suffix) {
  if (!message?.message_id) return;
  const original = message.text || message.caption || "Codex approval";
  const withoutKeyboard = `${original}\n\n${suffix}`;
  await telegram("editMessageText", {
    chat_id: message.chat?.id || TELEGRAM_CHAT_ID,
    message_id: message.message_id,
    text: html(withoutKeyboard),
    parse_mode: "HTML",
    reply_markup: { inline_keyboard: [] },
  }).catch(async () => {
    await telegram("editMessageReplyMarkup", {
      chat_id: message.chat?.id || TELEGRAM_CHAT_ID,
      message_id: message.message_id,
      reply_markup: { inline_keyboard: [] },
    });
  });
}

function mirrorNotification(msg) {
  if (!MIRROR_AGENT_MESSAGES) return;

  if (msg.method === "agent/message/delta") {
    bufferTelegram(`agent:${msg.params?.threadId || "default"}`, msg.params?.delta || "");
    return;
  }

  if (msg.method === "turn/completed") {
    flushAllTelegramBuffers();
    return;
  }

  if (msg.method === "item/commandExecution/outputDelta") {
    const delta = msg.params?.delta;
    if (delta && delta.length < 240) {
      bufferTelegram(`cmd:${msg.params?.itemId || "default"}`, delta);
    }
  }
}

function bufferTelegram(key, delta) {
  if (!delta) return;
  const existing = agentBuffers.get(key) || { text: "", timer: null };
  existing.text += delta;
  if (existing.text.length > 3000) {
    flushTelegramBuffer(key);
  } else if (!existing.timer) {
    existing.timer = setTimeout(() => flushTelegramBuffer(key), 1200);
  }
  agentBuffers.set(key, existing);
}

function flushAllTelegramBuffers() {
  for (const key of agentBuffers.keys()) flushTelegramBuffer(key);
}

function flushTelegramBuffer(key) {
  const buffer = agentBuffers.get(key);
  if (!buffer) return;
  clearTimeout(buffer.timer);
  agentBuffers.delete(key);

  const text = buffer.text.trim();
  if (!text) return;

  const chunks = splitTelegram(text, 3500);
  for (const chunk of chunks) {
    telegram("sendMessage", {
      chat_id: TELEGRAM_CHAT_ID,
      text: chunk,
      disable_web_page_preview: true,
    }).catch((error) => console.error("[telegram] mirror failed:", error.message));
  }
}

async function startTelegramPolling() {
  console.log("[telegram] polling started");
  for (;;) {
    try {
      const updates = await telegram("getUpdates", {
        offset: telegramOffset,
        timeout: 50,
        allowed_updates: ["message", "callback_query"],
      }, 60_000);

      for (const update of updates) {
        telegramOffset = update.update_id + 1;
        if (update.callback_query) {
          await handleTelegramCallback(update.callback_query);
        } else if (update.message && String(update.message.chat?.id) === String(TELEGRAM_CHAT_ID)) {
          await handleTelegramMessage(update.message);
        }
      }
    } catch (error) {
      console.error("[telegram] polling error:", error.message);
      await sleep(3000);
    }
  }
}

async function handleTelegramMessage(message) {
  const text = (message.text || "").trim();
  if (text === "/start" || text === "/help") {
    await telegram("sendMessage", {
      chat_id: TELEGRAM_CHAT_ID,
      text: [
        "Codex bridge is online.",
        "",
        "Use your screen Codex client normally.",
        "Approval requests will appear here with buttons.",
      ].join("\n"),
    });
  }
}

async function telegram(method, payload, timeoutMs = 15_000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const json = await response.json();
    if (!json.ok) {
      throw new Error(`${method}: ${json.description || response.statusText}`);
    }
    return json.result;
  } finally {
    clearTimeout(timeout);
  }
}

function acceptWebSocket(req, socket, head) {
  if (head?.length) {
    socket.destroy();
    throw new Error("Unexpected HTTP upgrade body");
  }

  const key = req.headers["sec-websocket-key"];
  const accept = createHash("sha1")
    .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
    .digest("base64");

  socket.write([
    "HTTP/1.1 101 Switching Protocols",
    "Upgrade: websocket",
    "Connection: Upgrade",
    `Sec-WebSocket-Accept: ${accept}`,
    "\r\n",
  ].join("\r\n"));

  const ws = {
    socket,
    open: true,
    queue: [],
    id: randomBytes(6).toString("hex"),
    sendText(text) {
      if (!this.open) return;
      socket.write(encodeFrame(Buffer.from(text), false));
    },
    flushQueue() {
      const upstream = upstreamByClient.get(this);
      if (!upstream || upstream.readyState !== WebSocket.OPEN) return;
      for (const item of this.queue.splice(0)) upstream.send(item);
    },
  };

  let buffer = Buffer.alloc(0);
  socket.on("data", (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    for (;;) {
      const frame = decodeFrame(buffer);
      if (!frame) break;
      buffer = buffer.subarray(frame.bytes);
      if (frame.opcode === 0x8) {
        ws.open = false;
        socket.end();
        ws.onClose?.();
        return;
      }
      if (frame.opcode === 0x9) {
        socket.write(encodeFrame(frame.payload, false, 0xA));
        continue;
      }
      if (frame.opcode === 0x1) {
        ws.onMessage?.(frame.payload);
      }
    }
  });
  socket.on("close", () => {
    ws.open = false;
    ws.onClose?.();
  });
  socket.on("error", (error) => ws.onError?.(error));

  return ws;
}

function sendUpstream(upstream, text) {
  if (upstream.readyState === WebSocket.OPEN) {
    upstream.send(text);
  } else {
    for (const [client, candidate] of upstreamByClient.entries()) {
      if (candidate === upstream) client.queue.push(text);
    }
  }
}

function closePair(client, upstream) {
  if (client.open) client.socket.end();
  try {
    if (upstream.readyState === WebSocket.OPEN || upstream.readyState === WebSocket.CONNECTING) upstream.close();
  } catch {}
  upstreamByClient.delete(client);
}

function encodeFrame(payload, masked, opcode = 0x1) {
  const length = payload.length;
  const headerLength = length < 126 ? 2 : length < 65536 ? 4 : 10;
  const maskLength = masked ? 4 : 0;
  const frame = Buffer.alloc(headerLength + maskLength + length);
  frame[0] = 0x80 | opcode;

  if (length < 126) {
    frame[1] = (masked ? 0x80 : 0) | length;
    payload.copy(frame, headerLength + maskLength);
  } else if (length < 65536) {
    frame[1] = (masked ? 0x80 : 0) | 126;
    frame.writeUInt16BE(length, 2);
    payload.copy(frame, headerLength + maskLength);
  } else {
    frame[1] = (masked ? 0x80 : 0) | 127;
    frame.writeBigUInt64BE(BigInt(length), 2);
    payload.copy(frame, headerLength + maskLength);
  }

  return frame;
}

function decodeFrame(buffer) {
  if (buffer.length < 2) return null;
  const opcode = buffer[0] & 0x0f;
  const masked = Boolean(buffer[1] & 0x80);
  let length = buffer[1] & 0x7f;
  let offset = 2;

  if (length === 126) {
    if (buffer.length < offset + 2) return null;
    length = buffer.readUInt16BE(offset);
    offset += 2;
  } else if (length === 127) {
    if (buffer.length < offset + 8) return null;
    length = Number(buffer.readBigUInt64BE(offset));
    offset += 8;
  }

  const maskOffset = offset;
  if (masked) offset += 4;
  if (buffer.length < offset + length) return null;

  let payload = Buffer.from(buffer.subarray(offset, offset + length));
  if (masked) {
    const mask = buffer.subarray(maskOffset, maskOffset + 4);
    for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
  }

  return { opcode, payload, bytes: offset + length };
}

function requestKey(client, requestId) {
  return `${client.id}:${String(requestId)}`;
}

function shortApprovalToken(key) {
  return createHash("sha256").update(key).digest("base64url").slice(0, 20);
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function html(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/&lt;(\/?)(b|code)&gt;/g, "<$1$2>");
}

function splitTelegram(text, size) {
  const chunks = [];
  for (let i = 0; i < text.length; i += size) chunks.push(text.slice(i, i + size));
  return chunks;
}

function sleep(ms) {
  return new Promise((resolveSleep) => setTimeout(resolveSleep, ms));
}

function loadEnv() {
  const output = { ...process.env };
  const path = resolve(process.cwd(), ".env");
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

function fatal(message) {
  console.error(`[bridge] ${message}`);
  process.exit(1);
}
