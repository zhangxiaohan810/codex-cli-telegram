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
const PTY_CONTROL_HOST = env.PTY_CONTROL_HOST || "127.0.0.1";
const PTY_CONTROL_PORT = Number(env.PTY_CONTROL_PORT || 8767);
const TELEGRAM_INPUT_MODE = env.TELEGRAM_INPUT_MODE || "tui";
const MIRROR_AGENT_MESSAGES = env.MIRROR_AGENT_MESSAGES !== "0";
const MIRROR_PROCESS_EVENTS = env.MIRROR_PROCESS_EVENTS === "1";
const INCLUDE_APPROVAL_PARAMS = env.INCLUDE_APPROVAL_PARAMS === "1";
const BRIDGE_DEBUG_RPC = env.BRIDGE_DEBUG_RPC === "1";
const TELEGRAM_PROXY = env.TELEGRAM_PROXY || env.HTTPS_PROXY || env.HTTP_PROXY || "";
const TELEGRAM_CODE_LIMIT = 2500;
const TELEGRAM_RETRIES = Number(env.TELEGRAM_RETRIES || 2);
const TELEGRAM_SAFE_MESSAGE_LIMIT = Number(env.TELEGRAM_SAFE_MESSAGE_LIMIT || 3800);
if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
  fatal("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required. Copy .env.example to .env first.");
}

const upstreamByClient = new Map();
const pendingApprovals = new Map();
const pendingBridgeRequests = new Map();
const seenClientResponses = new Set();
const agentBuffers = new Map();
const streamedItems = new Set();
const fileChangePatches = new Map();
let activeClient = null;
let activeThreadId = null;
let activeTurnId = null;
let activeCwd = null;
let requestSeq = 10_000;
let telegramOffset = 0;
let telegramMessageQueue = Promise.resolve();

setupTelegramCommands().catch((error) => {
  console.error("[telegram] failed to set command menu:", error.message);
});

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
  const isActiveClient = !activeClient?.open;
  const upstream = new WebSocket(CODEX_UPSTREAM_WS);
  upstreamByClient.set(client, upstream);
  if (isActiveClient) activeClient = client;

  console.log(`[bridge] ${isActiveClient ? "active" : "auxiliary"} client connected id=${client.id}, upstream=${CODEX_UPSTREAM_WS}`);

  client.onMessage = (data) => handleClientMessage(client, upstream, data, isActiveClient);
  upstream.addEventListener("message", (event) => handleUpstreamMessage(client, upstream, event.data, isActiveClient));

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
    console.error("[bridge] upstream error:", describeError(error));
    closePair(client, upstream);
  });
});

server.listen(BRIDGE_PORT, BRIDGE_HOST, () => {
  console.log(`[bridge] listening ws://${BRIDGE_HOST}:${BRIDGE_PORT}`);
  console.log(`[bridge] connect screen client with: codex --remote ws://${BRIDGE_HOST}:${BRIDGE_PORT}`);
  if (TELEGRAM_PROXY) console.log(`[telegram] proxy=${TELEGRAM_PROXY}`);
});

function handleClientMessage(client, upstream, data, isActiveClient = true) {
  const text = data.toString();
  const msg = parseJson(text);

  if (msg?.id !== undefined && seenClientResponses.has(requestKey(client, msg.id))) {
    console.log(`[bridge] swallowed duplicate response id=${msg.id}`);
    return;
  }

  if (isActiveClient) {
    trackClientRequest(msg);
    if (MIRROR_PROCESS_EVENTS) mirrorClientRequest(msg);
  }
  sendUpstream(upstream, text);
}

function trackClientRequest(msg) {
  if (!msg?.method) return;
  if (msg.method === "turn/start" || msg.method === "turn/steer") {
    activeThreadId = msg.params?.threadId || activeThreadId;
    activeCwd = msg.params?.cwd || activeCwd;
  }
  if (msg.method === "thread/start" || msg.method === "thread/resume") {
    activeCwd = msg.params?.cwd || activeCwd;
  }
}

function mirrorClientRequest(msg) {
  if (!MIRROR_PROCESS_EVENTS || !msg?.method) return;
  if (msg.method === "turn/start" || msg.method === "turn/steer" || msg.method === "thread/start") {
    sendTelegramText(formatEventMessage(`client request: ${msg.method}`, msg.params));
  }
}

function handleUpstreamMessage(client, upstream, data, isActiveClient = true) {
  const text = data.toString();
  const msg = parseJson(text);

  if (isActiveClient && handleBridgeResponse(msg)) return;

  if (msg && isActiveClient) {
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

function handleBridgeResponse(msg) {
  if (!msg?.id || !pendingBridgeRequests.has(msg.id)) return false;
  const request = pendingBridgeRequests.get(msg.id);
  pendingBridgeRequests.delete(msg.id);
  logRpc("bridge response", { id: msg.id, method: request.method, error: msg.error, result: summarizeRpcResult(msg.result) });
  request.resolve(msg);
  return true;
}

function trackServerNotification(msg) {
  if (msg.method === "thread/started") {
    activeThreadId = msg.params?.thread?.id || activeThreadId;
    activeCwd = msg.params?.thread?.cwd || activeCwd;
    return;
  }

  if (msg.method === "turn/started") {
    activeThreadId = msg.params?.threadId || activeThreadId;
    activeTurnId = msg.params?.turn?.id || activeTurnId;
    activeCwd = msg.params?.cwd || activeCwd;
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

  const result = await sendTelegramMessage({
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

  if (data.startsWith("m:")) {
    await handleScreenOnlyCallback(query, "/model");
    return;
  }

  if (data.startsWith("r:")) {
    await handleScreenOnlyCallback(query, "/resume");
    return;
  }

  if (data.startsWith("e:")) {
    await handleScreenOnlyCallback(query, "/reasoning");
    return;
  }

  if (data.startsWith("ap:")) {
    await handleScreenOnlyCallback(query, "/approvals");
    return;
  }

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

async function handleScreenOnlyCallback(query, command) {
  try {
    await sendPtyControl({ action: "submit", text: command });
    await telegram("editMessageText", {
      chat_id: query.message.chat?.id || TELEGRAM_CHAT_ID,
      message_id: query.message.message_id,
      text: `Sent to screen Codex CLI: ${command}`,
      reply_markup: { inline_keyboard: [] },
    });
  } catch (error) {
    await editTelegramApproval(query.message, `Failed to send input to screen Codex CLI: ${error.message}`);
  }
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
    sendTelegramBlock("assistant", item.text);
    streamedItems.add(item.id);
    return;
  }

  if (item.type === "plan" && item.text) {
    sendTelegramBlock("plan", item.text);
    streamedItems.add(item.id);
    return;
  }

  if (item.type === "reasoning") {
    const text = [...(item.summary || []), ...(item.content || [])].filter(Boolean).join("\n");
    if (text) {
      sendTelegramBlock("reasoning", text);
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

  sendTelegramBlock(buffer.label, text);
}

function sendTelegramText(text) {
  const payloads = splitTelegramHtmlMessage(text).map((chunk) => ({
    chat_id: TELEGRAM_CHAT_ID,
    text: chunk,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  }));
  return enqueueTelegramMessages(payloads).catch((error) => console.error("[telegram] mirror failed:", error.message));
}

function sendTelegramBlock(label, text) {
  const chunks = splitTelegramBlock(label, text);
  const payloads = chunks.map((chunk, index) => {
    const suffix = chunks.length > 1 ? ` (${index + 1}/${chunks.length})` : "";
    return {
      chat_id: TELEGRAM_CHAT_ID,
      text: html(`<b>${label}${suffix}</b>\n<pre>${chunk}</pre>`),
      parse_mode: "HTML",
      disable_web_page_preview: true,
    };
  });
  return enqueueTelegramMessages(payloads).catch((error) => console.error("[telegram] mirror failed:", error.message));
}

function sendTelegramMessage(payload) {
  return enqueueTelegramMessages([payload]).then((results) => results[0]);
}

function enqueueTelegramMessages(payloads) {
  const previous = telegramMessageQueue;
  const task = previous.then(async () => {
    const results = [];
    for (const payload of payloads) {
      results.push(await telegram("sendMessage", payload));
    }
    return results;
  });
  telegramMessageQueue = task.catch(() => {});
  return task;
}

async function setupTelegramCommands() {
  await telegram("setMyCommands", {
    commands: [
      { command: "start", description: "Show bridge help" },
      { command: "bridge_help", description: "Show bridge help" },
      { command: "bridge_status", description: "Show bridge connection status" },
      { command: "help", description: "Type /help into screen Codex CLI" },
      { command: "model", description: "Type /model into screen Codex CLI" },
      { command: "reasoning", description: "Type /reasoning into screen Codex CLI" },
      { command: "approvals", description: "Type /approvals into screen Codex CLI" },
      { command: "status", description: "Type /status into screen Codex CLI" },
      { command: "diff", description: "Type /diff into screen Codex CLI" },
      { command: "review", description: "Type /review into screen Codex CLI" },
      { command: "compact", description: "Type /compact into screen Codex CLI" },
      { command: "stop", description: "Codex: interrupt active turn" },
      { command: "new", description: "Type /new into screen Codex CLI" },
      { command: "resume", description: "Type /resume into screen Codex CLI" },
    ],
    scope: { type: "chat", chat_id: TELEGRAM_CHAT_ID },
  });
  console.log("[telegram] command menu configured");
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
  const text = normalizeTelegramCommand(message.text || "").trim();
  if (text.startsWith("/")) {
    await handleSlashCommand(text);
    return;
  }

  if (!text) return;
  if (TELEGRAM_INPUT_MODE === "tui") {
    await sendTextToScreenCli(text);
    return;
  }
  await injectTelegramText(text);
}

async function handleSlashCommand(text) {
  const [command] = text.split(/\s+/);

  switch (command) {
    case "/start":
    case "/bridge_help":
      await sendBridgeHelp();
      return;
    case "/bridge_status":
      await sendBridgeStatus();
      return;
    case "/stop":
      await stopActiveTurn();
      return;
    case "/help":
    case "/model":
    case "/reasoning":
    case "/approvals":
    case "/status":
    case "/diff":
    case "/review":
    case "/compact":
    case "/new":
    case "/resume":
      await sendSlashToScreenCli(text);
      return;
    default:
      await sendSlashToScreenCli(text);
  }
}

function normalizeTelegramCommand(text) {
  return text.replace(/^\/([A-Za-z0-9_]+)@[A-Za-z0-9_]+(\s|$)/, "/$1$2");
}

async function sendBridgeHelp() {
  await sendTelegramMessage({
    chat_id: TELEGRAM_CHAT_ID,
    text: [
      "Codex bridge is online.",
      "",
      "Type normal text to send it to Codex.",
      "Type / to open Telegram command suggestions.",
      "",
      "Bridge commands:",
      "/bridge_help - show this help",
      "/bridge_status - show bridge status",
      "",
      "Codex slash commands from Telegram are typed into the screen Codex CLI through the PTY driver, so screen-local state stays synchronized.",
      "Use /stop for bridge-level turn interrupt. Use /bridge_status for bridge diagnostics.",
    ].join("\n"),
  });
}

async function sendBridgeStatus() {
  const upstream = getActiveUpstream();
  let thread = null;
  let config = null;

  if (upstream && activeThreadId) {
    const threadResponse = await sendBridgeRequest(upstream, "thread/read", {
      threadId: activeThreadId,
      includeTurns: false,
    });
    if (!threadResponse.error) thread = threadResponse.result?.thread || null;
  }

  if (upstream) {
    const configResponse = await sendBridgeRequest(upstream, "config/read", {
      includeLayers: false,
      ...(activeCwd ? { cwd: activeCwd } : {}),
    });
    if (!configResponse.error) config = configResponse.result?.config || null;
  }

  await sendTelegramMessage({
    chat_id: TELEGRAM_CHAT_ID,
    text: [
      "Codex status",
      "",
      `cli connected: ${Boolean(activeClient)}`,
      `client id: ${activeClient?.id || "(none)"}`,
      `thread: ${activeThreadId || "(none)"}`,
      `thread status: ${formatThreadStatus(thread?.status) || "(unknown)"}`,
      `thread title: ${thread?.name || thread?.preview || "(none)"}`,
      `active turn: ${activeTurnId || "(none)"}`,
      `cwd: ${activeCwd || thread?.cwd || "(unknown)"}`,
      `config model: ${config?.model || "(default)"}`,
      `config reasoning: ${config?.model_reasoning_effort || "(default)"}`,
      `config approvals: ${formatApprovalPolicy(config?.approval_policy) || "(default)"}`,
      `telegram input mode: ${TELEGRAM_INPUT_MODE}`,
      `upstream: ${CODEX_UPSTREAM_WS}`,
      `bridge: ws://${BRIDGE_HOST}:${BRIDGE_PORT}`,
      `pty control: ${PTY_CONTROL_HOST}:${PTY_CONTROL_PORT}`,
    ].join("\n"),
  });
}

async function stopActiveTurn() {
  const upstream = getActiveUpstream();
  if (!upstream) {
    await sendTelegramText("Codex upstream is not connected.");
    return;
  }
  if (!activeThreadId || !activeTurnId) {
    await sendTelegramText("No active Codex turn is running.");
    return;
  }

  const response = await sendBridgeRequest(upstream, "turn/interrupt", {
    threadId: activeThreadId,
    turnId: activeTurnId,
  });
  if (response.error) {
    await sendTelegramText(`Failed to stop turn: ${response.error.message || JSON.stringify(response.error)}`);
    return;
  }

  await sendTelegramText("Stop request sent to Codex.");
}

async function sendSlashToScreenCli(text) {
  await submitScreenCliInput(text, `Sent to screen Codex CLI: ${text}`);
}

async function sendTextToScreenCli(text) {
  await submitScreenCliInput(text, `Sent to screen Codex CLI: ${text}`);
}

async function submitScreenCliInput(text, successMessage) {
  try {
    await sendPtyControl({ action: "submit", text });
    await sendTelegramText(successMessage);
  } catch (error) {
    await sendTelegramText(`Failed to send input to screen Codex CLI: ${error.message}`);
  }
}

function sendPtyControl(payload, timeoutMs = 3000) {
  const body = JSON.stringify(payload);
  return new Promise((resolveRequest, rejectRequest) => {
    const socket = netConnect({ host: PTY_CONTROL_HOST, port: PTY_CONTROL_PORT });
    const chunks = [];
    const timer = setTimeout(() => {
      socket.destroy();
      rejectRequest(new Error(`PTY control timed out at ${PTY_CONTROL_HOST}:${PTY_CONTROL_PORT}`));
    }, timeoutMs);

    socket.once("connect", () => {
      socket.end(body);
    });
    socket.on("data", (chunk) => chunks.push(chunk));
    socket.once("end", () => {
      clearTimeout(timer);
      const text = Buffer.concat(chunks).toString("utf8").trim();
      try {
        const response = JSON.parse(text || "{}");
        if (!response.ok) throw new Error(response.error || "PTY control request failed");
        resolveRequest(response);
      } catch (error) {
        rejectRequest(error);
      }
    });
    socket.once("error", (error) => {
      clearTimeout(timer);
      rejectRequest(error);
    });
  });
}

async function compactActiveThread() {
  const upstream = getActiveUpstream();
  if (!upstream) {
    await sendTelegramText("Codex upstream is not connected.");
    return;
  }
  if (!activeThreadId) {
    await sendTelegramText("No active Codex thread to compact.");
    return;
  }

  const response = await sendBridgeRequest(upstream, "thread/compact/start", { threadId: activeThreadId });
  if (response.error) {
    await sendTelegramText(`Failed to compact thread: ${response.error.message || JSON.stringify(response.error)}`);
    return;
  }

  await sendTelegramText("Compaction request sent to Codex.");
}

async function sendCurrentDiff() {
  const upstream = getActiveUpstream();
  if (!upstream) {
    await sendTelegramText("Codex upstream is not connected.");
    return;
  }
  if (!activeCwd) {
    await sendTelegramText("Current workspace cwd is unknown. Send one prompt from the screen CLI first.");
    return;
  }

  const response = await sendBridgeRequest(upstream, "gitDiffToRemote", { cwd: activeCwd });
  if (response.error) {
    await sendTelegramText(`Failed to load diff: ${response.error.message || JSON.stringify(response.error)}`);
    return;
  }

  const diff = response.result?.diff || "";
  const sha = response.result?.sha || "";
  if (!diff.trim()) {
    await sendTelegramText(`No diff from remote${sha ? ` (${sha})` : ""}.`);
    return;
  }

  await sendTelegramBlock(`git diff${sha ? ` ${sha}` : ""}`, diff);
}

async function handleReasoningCommand(argument) {
  await sendScreenOnlyCommandNotice("/reasoning", argument);
}

async function handleApprovalPolicyCommand(argument) {
  await sendScreenOnlyCommandNotice("/approvals", argument);
}

async function startReview(argument) {
  const upstream = getActiveUpstream();
  if (!upstream) {
    await sendTelegramText("Codex upstream is not connected.");
    return;
  }
  if (!activeThreadId) {
    await sendTelegramText("No active Codex thread to review.");
    return;
  }

  const target = argument
    ? { type: "custom", instructions: argument }
    : { type: "uncommittedChanges" };
  const response = await sendBridgeRequest(upstream, "review/start", {
    threadId: activeThreadId,
    target,
    delivery: "inline",
  });
  if (response.error) {
    await sendTelegramText(`Failed to start review: ${response.error.message || JSON.stringify(response.error)}`);
    return;
  }

  activeTurnId = response.result?.turn?.id || activeTurnId;
  await sendTelegramText([
    "Review started in Codex.",
    "",
    `thread: ${response.result?.reviewThreadId || activeThreadId}`,
    `turn: ${activeTurnId || "(unknown)"}`,
  ].join("\n"));
}

function getActiveUpstream() {
  if (!activeClient) return null;
  return upstreamByClient.get(activeClient) || null;
}

function sendBridgeRequest(upstream, method, params) {
  const id = nextRequestId();
  const request = { jsonrpc: "2.0", id, method, params };
  return new Promise((resolveRequest) => {
    const timeout = setTimeout(() => {
      pendingBridgeRequests.delete(id);
      resolveRequest({ error: { message: `${method} timed out` } });
    }, 10_000);

    pendingBridgeRequests.set(id, {
      method,
      resolve: (message) => {
        clearTimeout(timeout);
        resolveRequest(message);
      },
    });

    logRpc("bridge request", { id, method, params });
    sendUpstream(upstream, JSON.stringify(request));
  });
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

function formatThreadButton(thread) {
  const title = thread.name || thread.preview || thread.id;
  const date = thread.updatedAt ? new Date(thread.updatedAt * 1000).toISOString().slice(0, 10) : "";
  return truncateSingleLine(`${date} ${title}`, 58);
}

function formatThreadStatus(status) {
  if (!status) return "";
  if (typeof status === "string") return status;
  if (status.type === "active") {
    const flags = Array.isArray(status.activeFlags) ? status.activeFlags.join(", ") : "";
    return flags ? `active (${flags})` : "active";
  }
  return status.type || JSON.stringify(status);
}

function formatApprovalPolicy(policy) {
  if (!policy) return "";
  return typeof policy === "string" ? policy : JSON.stringify(policy);
}

async function telegram(method, payload, timeoutMs = 15_000) {
  const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/${method}`;
  let lastError = null;

  for (let attempt = 0; attempt <= TELEGRAM_RETRIES; attempt += 1) {
    try {
      const json = TELEGRAM_PROXY
        ? await postJsonViaHttpProxy(url, payload, TELEGRAM_PROXY, timeoutMs)
        : await postJsonDirect(url, payload, timeoutMs);
      if (!json.ok) {
        throw new Error(`${method}: ${json.description || "request failed"}`);
      }
      return json.result;
    } catch (error) {
      lastError = error;
      if (attempt >= TELEGRAM_RETRIES || !isRetryableTelegramError(error)) break;
      const delay = 500 * (attempt + 1);
      console.warn(`[telegram] ${method} failed (${describeError(error)}), retrying in ${delay}ms`);
      await sleep(delay);
    }
  }

  throw lastError;
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
    sendClose(code = 1000, reason = "") {
      if (!this.open) return;
      this.open = false;
      const reasonBuffer = Buffer.from(reason);
      const payload = Buffer.alloc(2 + reasonBuffer.length);
      payload.writeUInt16BE(code, 0);
      reasonBuffer.copy(payload, 2);
      socket.write(encodeFrame(payload, false, 0x8), () => socket.end());
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
    console.log(`[bridge] active client disconnected id=${client.id}`);
    activeClient = null;
    activeThreadId = null;
    activeTurnId = null;
    activeCwd = null;
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

function splitTelegramHtmlMessage(text) {
  const value = String(text || "");
  if (value.length <= TELEGRAM_SAFE_MESSAGE_LIMIT) return [value];

  const plain = value.replace(/<\/?(b|code|pre)>/g, "");
  const chunks = splitPlainTextByHtmlBudget(plain, (chunk, suffix) => html(`<pre>${suffix}${chunk}</pre>`).length);
  return chunks.map((chunk, index) => {
    const suffix = chunks.length > 1 ? `[${index + 1}/${chunks.length}]\n` : "";
    return html(`<pre>${suffix}${chunk}</pre>`);
  });
}

function splitTelegramBlock(label, text) {
  return splitPlainTextByHtmlBudget(String(text || ""), (chunk, suffix) => {
    return html(`<b>${label}${suffix}</b>\n<pre>${chunk}</pre>`).length;
  });
}

function splitPlainTextByHtmlBudget(text, measure) {
  if (!text) return [""];
  const chunks = [];
  let remaining = text;
  const suffixBudget = " (999/999)";

  while (remaining.length) {
    const size = fittingPrefixSize(remaining, (candidate) => measure(candidate, suffixBudget) <= TELEGRAM_SAFE_MESSAGE_LIMIT);
    if (size >= remaining.length) {
      chunks.push(remaining);
      break;
    }

    const breakpoint = findReadableBreakpoint(remaining, size);
    chunks.push(remaining.slice(0, breakpoint).trimEnd());
    remaining = remaining.slice(breakpoint).trimStart();
  }

  return chunks;
}

function fittingPrefixSize(text, fits) {
  let low = 1;
  let high = text.length;
  let best = 1;

  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    if (fits(text.slice(0, mid))) {
      best = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }

  return best;
}

function findReadableBreakpoint(text, maxSize) {
  const minSize = Math.floor(maxSize * 0.6);
  const slice = text.slice(0, maxSize);
  const newline = slice.lastIndexOf("\n");
  if (newline >= minSize) return newline + 1;
  const space = slice.lastIndexOf(" ");
  if (space >= minSize) return space + 1;
  return maxSize;
}

function truncate(text, size) {
  if (!text || text.length <= size) return text || "";
  return `${text.slice(0, size - 28)}\n... truncated ${text.length - size + 28} chars`;
}

function truncateSingleLine(text, size) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (normalized.length <= size) return normalized;
  return `${normalized.slice(0, size - 3)}...`;
}

function describeError(error) {
  if (!error) return "(empty error)";
  if (error.message) return error.message;
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

function isRetryableTelegramError(error) {
  const message = describeError(error);
  return [
    "ECONNRESET",
    "ETIMEDOUT",
    "ECONNREFUSED",
    "EAI_AGAIN",
    "socket hang up",
    "request timeout",
    "proxy timeout",
  ].some((pattern) => message.includes(pattern));
}

function summarizeRpcResult(result) {
  if (!result || typeof result !== "object") return result;
  if (Array.isArray(result)) return { length: result.length };
  const summary = {};
  for (const key of Object.keys(result).slice(0, 8)) {
    const value = result[key];
    if (Array.isArray(value)) summary[key] = `[${value.length} items]`;
    else if (value && typeof value === "object") summary[key] = value.id ? `{id:${value.id}}` : "{object}";
    else summary[key] = value;
  }
  return summary;
}

function logRpc(label, payload) {
  if (!BRIDGE_DEBUG_RPC) return;
  console.log(`[bridge] ${label}: ${JSON.stringify(payload)}`);
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
