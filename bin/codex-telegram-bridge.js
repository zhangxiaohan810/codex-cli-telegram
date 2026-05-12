#!/usr/bin/env node

import { createHash, randomBytes } from "node:crypto";
import { createServer } from "node:http";
import { request as httpsRequest } from "node:https";
import { connect as netConnect } from "node:net";
import { connect as tlsConnect } from "node:tls";
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { URL } from "node:url";

const env = loadEnv();

const TELEGRAM_BOT_TOKEN = env.TELEGRAM_BOT_TOKEN;
const TELEGRAM_CHAT_ID = env.TELEGRAM_CHAT_ID;
const CODEX_UPSTREAM_WS = env.CODEX_UPSTREAM_WS || "ws://127.0.0.1:8765";
const BRIDGE_HOST = env.BRIDGE_HOST || "127.0.0.1";
const BRIDGE_PORT = Number(env.BRIDGE_PORT || 8766);
const MIRROR_AGENT_MESSAGES = env.MIRROR_AGENT_MESSAGES !== "0";
const MIRROR_PROCESS_EVENTS = env.MIRROR_PROCESS_EVENTS === "1";
const INCLUDE_APPROVAL_PARAMS = env.INCLUDE_APPROVAL_PARAMS === "1";
const TELEGRAM_PROXY = env.TELEGRAM_PROXY || env.HTTPS_PROXY || env.HTTP_PROXY || "";
const TELEGRAM_MESSAGE_LIMIT = 3500;
const TELEGRAM_CODE_LIMIT = 2500;

if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
  fatal("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required. Copy .env.example to .env first.");
}

const upstreamByClient = new Map();
const pendingApprovals = new Map();
const seenClientResponses = new Set();
const agentBuffers = new Map();
const streamedItems = new Set();
const fileChangePatches = new Map();
let activeClient = null;
let activeThreadId = null;
let activeTurnId = null;
let requestSeq = 10_000;
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
  activeClient = client;

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
  if (TELEGRAM_PROXY) console.log(`[telegram] proxy=${TELEGRAM_PROXY}`);
});

function handleClientMessage(client, upstream, data) {
  const text = data.toString();
  const msg = parseJson(text);

  if (msg?.id !== undefined && seenClientResponses.has(requestKey(client, msg.id))) {
    console.log(`[bridge] swallowed duplicate response id=${msg.id}`);
    return;
  }

  trackClientRequest(msg);
  if (MIRROR_PROCESS_EVENTS) mirrorClientRequest(msg);
  sendUpstream(upstream, text);
}

function trackClientRequest(msg) {
  if (!msg?.method) return;
  if (msg.method === "turn/start" || msg.method === "turn/steer") {
    activeThreadId = msg.params?.threadId || activeThreadId;
  }
}

function mirrorClientRequest(msg) {
  if (!MIRROR_PROCESS_EVENTS || !msg?.method) return;
  if (msg.method === "turn/start" || msg.method === "turn/steer" || msg.method === "thread/start") {
    sendTelegramText(formatEventMessage(`client request: ${msg.method}`, msg.params));
  }
}

function handleUpstreamMessage(client, upstream, data) {
  const text = data.toString();
  const msg = parseJson(text);

  if (msg) {
    if (msg.id !== undefined && msg.method) {
      console.log(`[bridge] server request method=${msg.method} id=${msg.id}`);
    }
    if (isApprovalRequest(msg)) {
      registerApproval(client, upstream, msg);
    } else if (msg.method === "serverRequest/resolved") {
      markResolved(client, msg.params?.requestId, "screen");
    } else {
      trackServerNotification(msg);
      mirrorNotification(msg);
    }
  }

  client.sendText(text);
}

function trackServerNotification(msg) {
  if (msg.method === "thread/started") {
    activeThreadId = msg.params?.thread?.id || activeThreadId;
    return;
  }

  if (msg.method === "turn/started") {
    activeThreadId = msg.params?.threadId || activeThreadId;
    activeTurnId = msg.params?.turn?.id || activeTurnId;
    return;
  }

  if (msg.method === "turn/completed") {
    activeThreadId = msg.params?.threadId || activeThreadId;
    if (msg.params?.turn?.id === activeTurnId) activeTurnId = null;
  }
}

function isApprovalRequest(msg) {
  return msg?.id !== undefined && [
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
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
  console.log(`[bridge] approval request method=${msg.method} id=${msg.id}`);
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
  const details = INCLUDE_APPROVAL_PARAMS ? formatRequestDetails(record) : "";
  if (record.method === "item/commandExecution/requestApproval") {
    return html([
      "<b>Codex command approval</b>",
      "",
      `cwd: <code>${p.cwd || ""}</code>`,
      `cmd: <code>${p.command || "(command unavailable)"}</code>`,
      p.reason ? `reason: ${p.reason}` : "",
      p.proposedExecpolicyAmendment ? `policy: <code>${JSON.stringify(p.proposedExecpolicyAmendment)}</code>` : "",
      details,
    ].filter(Boolean).join("\n"));
  }

  if (record.method === "execCommandApproval") {
    const command = Array.isArray(p.command) ? p.command.join(" ") : String(p.command || "(command unavailable)");
    return html([
      "<b>Codex command approval</b>",
      "",
      `cwd: <code>${p.cwd || ""}</code>`,
      `cmd: <code>${command}</code>`,
      p.reason ? `reason: ${p.reason}` : "",
      details,
    ].filter(Boolean).join("\n"));
  }

  if (record.method === "item/fileChange/requestApproval") {
    const patch = fileChangePatches.get(p.itemId);
    return html([
      "<b>Codex file-change approval</b>",
      "",
      patch ? formatPatchSummary(patch.changes) : "File changes are pending approval.",
      p.reason ? `reason: ${p.reason}` : "",
      details,
    ].filter(Boolean).join("\n"));
  }

  if (record.method === "applyPatchApproval") {
    const files = Object.keys(p.fileChanges || {});
    return html([
      "<b>Codex patch approval</b>",
      "",
      formatLegacyPatchSummary(p.fileChanges || {}, files),
      p.reason ? `reason: ${p.reason}` : "",
      details,
    ].filter(Boolean).join("\n"));
  }

  return html([
    "<b>Codex permissions approval</b>",
    "",
    "Telegram mirror is read-only for this request type. Approve or deny it on the screen client.",
    "",
    `cwd: <code>${p.cwd || ""}</code>`,
    p.reason ? `reason: ${p.reason}` : "",
    `permissions: <code>${JSON.stringify(p.permissions || {})}</code>`,
    details,
  ].filter(Boolean).join("\n"));
}

function formatPatchSummary(changes = []) {
  if (!changes.length) return "files: <code>(unknown)</code>";
  return changes.map((change) => {
    const kind = change.kind?.type || "update";
    const path = change.path || "(unknown)";
    const heading = `${kind}: ${path}`;
    const diff = change.diff ? `\n<pre>${truncate(change.diff, TELEGRAM_CODE_LIMIT)}</pre>` : "";
    return `<b>${heading}</b>${diff}`;
  }).join("\n\n");
}

function formatLegacyPatchSummary(fileChanges = {}, files = Object.keys(fileChanges)) {
  if (!files.length) return "files: <code>(unknown)</code>";
  return files.map((path) => {
    const change = fileChanges[path] || {};
    const kind = change.type || "update";
    const body = change.unified_diff || change.content || "";
    const diff = body ? `\n<pre>${truncate(body, TELEGRAM_CODE_LIMIT)}</pre>` : "";
    return `<b>${kind}: ${path}</b>${diff}`;
  }).join("\n\n");
}

function formatRequestDetails(record) {
  return [
    "",
    "<b>request params</b>",
    `<pre>${truncate(JSON.stringify(record.params || {}, null, 2), TELEGRAM_CODE_LIMIT)}</pre>`,
  ].join("\n");
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
    result: { decision: mapDecisionForMethod(record.method, decision) },
  };

  sendUpstream(record.upstream, JSON.stringify(response));
  markResolved(record.client, record.requestId, `telegram:${decision}`);
  await editTelegramApproval(query.message, `Resolved from Telegram: ${decision}`);
}

function mapDecisionForMethod(method, decision) {
  if (method === "execCommandApproval" || method === "applyPatchApproval") {
    return {
      accept: "approved",
      acceptForSession: "approved_for_session",
      decline: "denied",
      cancel: "abort",
    }[decision] || "denied";
  }
  return decision;
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
  if (msg.method === "item/fileChange/patchUpdated") {
    fileChangePatches.set(msg.params?.itemId, msg.params || {});
    return;
  }

  if (!MIRROR_AGENT_MESSAGES) return;

  if (msg.method === "item/agentMessage/delta") {
    bufferTelegram(`agent:${msg.params?.itemId || "default"}`, "assistant", msg.params?.delta || "");
    return;
  }

  if (msg.method === "item/plan/delta") {
    bufferTelegram(`plan:${msg.params?.itemId || "default"}`, "plan", msg.params?.delta || "");
    return;
  }

  if (msg.method === "item/reasoning/summaryTextDelta") {
    bufferTelegram(`reasoning:${msg.params?.itemId || "default"}`, "reasoning", msg.params?.delta || "");
    return;
  }

  if (msg.method === "turn/completed") {
    flushAllTelegramBuffers();
    return;
  }

  if (msg.method === "item/completed") {
    flushAllTelegramBuffers();
    mirrorCompletedTextItem(msg.params?.item);
    return;
  }

  if (MIRROR_PROCESS_EVENTS) mirrorProcessNotification(msg);
}

function mirrorCompletedTextItem(item) {
  if (!item?.id || streamedItems.has(item.id)) return;

  if (item.type === "agentMessage" && item.text) {
    sendTelegramText(html(`<b>assistant</b>\n<pre>${truncate(item.text, TELEGRAM_MESSAGE_LIMIT)}</pre>`));
    streamedItems.add(item.id);
    return;
  }

  if (item.type === "plan" && item.text) {
    sendTelegramText(html(`<b>plan</b>\n<pre>${truncate(item.text, TELEGRAM_MESSAGE_LIMIT)}</pre>`));
    streamedItems.add(item.id);
    return;
  }

  if (item.type === "reasoning") {
    const text = [...(item.summary || []), ...(item.content || [])].filter(Boolean).join("\n");
    if (text) {
      sendTelegramText(html(`<b>reasoning</b>\n<pre>${truncate(text, TELEGRAM_MESSAGE_LIMIT)}</pre>`));
      streamedItems.add(item.id);
    }
  }
}

function mirrorProcessNotification(msg) {
  if (msg.method === "turn/started") {
    sendTelegramText(formatEventMessage("turn started", msg.params));
    return;
  }

  if (msg.method === "item/started") {
    sendTelegramText(formatItemEvent("item started", msg.params));
    return;
  }

  if (msg.method === "item/commandExecution/outputDelta") {
    bufferTelegram(`cmd:${msg.params?.itemId || "default"}`, "cmd output", msg.params?.delta || "");
    return;
  }

  if (msg.method === "item/commandExecution/terminalInteraction") {
    sendTelegramText(formatEventMessage("terminal interaction", msg.params));
  }
}

function formatEventMessage(title, params) {
  return html([
    `<b>${title}</b>`,
    `<pre>${truncate(JSON.stringify(params || {}, null, 2), TELEGRAM_CODE_LIMIT)}</pre>`,
  ].join("\n"));
}

function formatItemEvent(title, params) {
  const item = params?.item || {};
  const type = item.type || item.kind || item.status || "";
  const summary = summarizeItem(item);
  return html([
    `<b>${title}${type ? `: ${type}` : ""}</b>`,
    summary ? "" : `<pre>${truncate(JSON.stringify(params || {}, null, 2), TELEGRAM_CODE_LIMIT)}</pre>`,
    summary,
  ].filter(Boolean).join("\n"));
}

function summarizeItem(item) {
  const candidates = [
    item.command,
    item.text,
    item.message,
    item.title,
    item.name,
    item.status,
  ].filter(Boolean);
  const text = candidates.length ? candidates.map((value) => typeof value === "string" ? value : JSON.stringify(value)).join("\n") : "";
  return text ? `<pre>${truncate(text, TELEGRAM_CODE_LIMIT)}</pre>` : "";
}

function bufferTelegram(key, label, delta) {
  if (!delta) return;
  const itemId = key.includes(":") ? key.split(":").slice(1).join(":") : key;
  if (itemId) streamedItems.add(itemId);
  const existing = agentBuffers.get(key) || { label, text: "", timer: null };
  existing.label = label;
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

  const chunks = splitTelegram(text, TELEGRAM_MESSAGE_LIMIT);
  for (const chunk of chunks) {
    sendTelegramText(html(`<b>${buffer.label}</b>\n<pre>${truncate(chunk, TELEGRAM_MESSAGE_LIMIT)}</pre>`));
  }
}

function sendTelegramText(text) {
  return telegram("sendMessage", {
    chat_id: TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  }).catch((error) => console.error("[telegram] mirror failed:", error.message));
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
    return;
  }

  if (!text) return;
  await injectTelegramText(text);
}

async function injectTelegramText(text) {
  if (!activeClient) {
    await sendTelegramText("No active Codex CLI client is connected. Start it with: codex --remote ws://127.0.0.1:8766");
    return;
  }

  if (!activeThreadId) {
    await sendTelegramText("No active Codex thread yet. Send one prompt from the Codex CLI first, then Telegram messages can continue it.");
    return;
  }

  const upstream = upstreamByClient.get(activeClient);
  if (!upstream) {
    await sendTelegramText("Codex upstream is not connected.");
    return;
  }

  const input = [{ type: "text", text, text_elements: [] }];
  const id = nextRequestId();
  const request = activeTurnId
    ? {
        jsonrpc: "2.0",
        id,
        method: "turn/steer",
        params: {
          threadId: activeThreadId,
          expectedTurnId: activeTurnId,
          input,
        },
      }
    : {
        jsonrpc: "2.0",
        id,
        method: "turn/start",
        params: {
          threadId: activeThreadId,
          input,
        },
      };

  sendUpstream(upstream, JSON.stringify(request));
  await sendTelegramText(`Sent to Codex: ${text}`);
}

async function telegram(method, payload, timeoutMs = 15_000) {
  const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/${method}`;
  const json = TELEGRAM_PROXY
    ? await postJsonViaHttpProxy(url, payload, TELEGRAM_PROXY, timeoutMs)
    : await postJsonDirect(url, payload, timeoutMs);
  if (!json.ok) {
    throw new Error(`${method}: ${json.description || "request failed"}`);
  }
  return json.result;
}

function postJsonDirect(url, payload, timeoutMs) {
  const body = JSON.stringify(payload);
  const target = new URL(url);
  return new Promise((resolveRequest, rejectRequest) => {
    const req = httpsRequest({
      hostname: target.hostname,
      port: target.port || 443,
      path: `${target.pathname}${target.search}`,
      method: "POST",
      headers: {
        "content-type": "application/json",
        "content-length": Buffer.byteLength(body),
      },
      timeout: timeoutMs,
    }, (res) => collectJson(res, resolveRequest, rejectRequest));

    req.on("timeout", () => req.destroy(new Error("request timeout")));
    req.on("error", rejectRequest);
    req.end(body);
  });
}

function postJsonViaHttpProxy(url, payload, proxyUrl, timeoutMs) {
  const body = JSON.stringify(payload);
  const target = new URL(url);
  const proxy = new URL(proxyUrl);

  return new Promise((resolveRequest, rejectRequest) => {
    const socket = netConnect(Number(proxy.port || 8080), proxy.hostname);
    socket.setTimeout(timeoutMs);
    socket.once("timeout", () => socket.destroy(new Error("proxy timeout")));
    socket.once("error", rejectRequest);

    socket.once("connect", () => {
      socket.write([
        `CONNECT ${target.hostname}:443 HTTP/1.1`,
        `Host: ${target.hostname}:443`,
        "Proxy-Connection: keep-alive",
        "",
        "",
      ].join("\r\n"));
    });

    let header = Buffer.alloc(0);
    const onProxyData = (chunk) => {
      header = Buffer.concat([header, chunk]);
      const end = header.indexOf("\r\n\r\n");
      if (end === -1) return;

      socket.off("data", onProxyData);
      const statusLine = header.subarray(0, end).toString("utf8").split("\r\n")[0] || "";
      if (!statusLine.includes(" 200 ")) {
        socket.destroy();
        rejectRequest(new Error(`proxy CONNECT failed: ${statusLine}`));
        return;
      }

      const rest = header.subarray(end + 4);
      const req = httpsRequest({
        hostname: target.hostname,
        port: 443,
        path: `${target.pathname}${target.search}`,
        method: "POST",
        createConnection: () => tlsConnect({
          socket,
          servername: target.hostname,
        }),
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(body),
        },
        timeout: timeoutMs,
      }, (res) => collectJson(res, resolveRequest, rejectRequest));

      req.on("timeout", () => req.destroy(new Error("request timeout")));
      req.on("error", rejectRequest);
      req.end(body);
      if (rest.length) socket.unshift(rest);
    };

    socket.on("data", onProxyData);
  });
}

function collectJson(res, resolveRequest, rejectRequest) {
  const chunks = [];
  res.on("data", (chunk) => chunks.push(chunk));
  res.on("end", () => {
    const body = Buffer.concat(chunks).toString("utf8");
    try {
      resolveRequest(JSON.parse(body));
    } catch {
      rejectRequest(new Error(`invalid JSON response: HTTP ${res.statusCode}`));
    }
  });
  res.on("error", rejectRequest);
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
  if (activeClient === client) {
    activeClient = null;
    activeTurnId = null;
  }
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

function nextRequestId() {
  requestSeq += 1;
  return `telegram-${requestSeq}`;
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
    .replace(/&lt;(\/?)(b|code|pre)&gt;/g, "<$1$2>");
}

function splitTelegram(text, size) {
  const chunks = [];
  for (let i = 0; i < text.length; i += size) chunks.push(text.slice(i, i + size));
  return chunks;
}

function truncate(text, size) {
  if (!text || text.length <= size) return text || "";
  return `${text.slice(0, size - 28)}\n... truncated ${text.length - size + 28} chars`;
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
