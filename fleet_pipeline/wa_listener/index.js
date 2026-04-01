/**
 * Fleet Tracker — WhatsApp real-time listener
 *
 * On every group message: HTTP POST directly to the Python API (instant pipeline).
 * Exposes a /health endpoint so the Python API can probe liveness.
 *
 * Usage:
 *   npm install
 *   node index.js
 *
 * First run: scan the QR code in the terminal to authenticate.
 * Session is saved to ./session/ and auto-reconnects.
 *
 * Env vars (all optional, set in ../../.env):
 *   WA_GROUP_JID       — WhatsApp group JID to listen to
 *   FLEET_API_URL      — Python API base URL   (default: http://localhost:8000)
 *   WA_HEALTH_PORT     — port for /health endpoint (default: 3001)
 *   WA_SESSION_DIR     — session storage path
 *   LOG_LEVEL          — pino log level (default: info)
 */

"use strict";

require("dotenv").config({ path: require("path").resolve(__dirname, "../../.env") });

const http       = require("http");
const path       = require("path");
const fs         = require("fs");
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require("@whiskeysockets/baileys");
const pino       = require("pino");
const qrcode     = require("qrcode-terminal");

// ── Config ───────────────────────────────────────────────────────────────────

const GROUP_JID    = process.env.WA_GROUP_JID    || "";
const SESSION_DIR  = process.env.WA_SESSION_DIR  || path.join(__dirname, "session");
const LOG_LEVEL    = process.env.LOG_LEVEL       || "info";
const API_URL      = process.env.FLEET_API_URL   || "http://localhost:8000";
const HEALTH_PORT  = parseInt(process.env.WA_HEALTH_PORT || "3001", 10);
const LOGS_DIR     = process.env.FLEET_LOGS_DIR  || "/logs";

// ── File logging — tee console.* to LOGS_DIR/wa.log ─────────────────────────

let _logStream = null;
try {
    fs.mkdirSync(LOGS_DIR, { recursive: true });
    _logStream = fs.createWriteStream(path.join(LOGS_DIR, "wa.log"), { flags: "a" });
} catch (_e) { /* can't open log file — stdout only */ }

function _writeLine(level, args) {
    const msg = args.map(a => (typeof a === "string" ? a : JSON.stringify(a))).join(" ");
    const line = `${new Date().toISOString()} [${level}] ${msg}\n`;
    process.stdout.write(line);
    if (_logStream) _logStream.write(line);
}

console.log   = (...a) => _writeLine("INFO",  a);
console.warn  = (...a) => _writeLine("WARN",  a);
console.error = (...a) => _writeLine("ERROR", a);

// ── Shift signal detection (mirrors Python Level2) ───────────────────────────

const SHIFT_SIGNAL_PATTERNS = [
  /\bshift\s+(start|started|begin|begins|end|over|ended)\b/i,
  /\bs[123]\b/i,
  /\bshift\s+[123]\b/i,
];

function detectMessageType(text) {
  if (!text) return "unknown";
  for (const pat of SHIFT_SIGNAL_PATTERNS) {
    if (pat.test(text)) return "shift_signal";
  }
  return "fleet_event";
}

// ── HTTP helpers ─────────────────────────────────────────────────────────────

function postToAPI(endpoint, body) {
  return new Promise((resolve) => {
    const data   = JSON.stringify(body);
    const apiUrl = new URL(endpoint, API_URL);
    const options = {
      hostname: apiUrl.hostname,
      port:     apiUrl.port || 80,
      path:     apiUrl.pathname,
      method:   "POST",
      headers:  { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) },
    };
    const req = http.request(options, (res) => {
      let raw = "";
      res.on("data", (chunk) => raw += chunk);
      res.on("end", () => resolve({ status: res.statusCode, body: raw }));
    });
    req.on("error", (err) => {
      console.error(`[Fleet WA Listener] API POST ${endpoint} failed: ${err.message}`);
      resolve({ status: 0, body: "" });
    });
    req.setTimeout(10000, () => { req.destroy(); resolve({ status: 0, body: "" }); });
    req.write(data);
    req.end();
  });
}

// ── Active socket (set on each successful connect) ────────────────────────────

let isConnected = false;
let activeSock  = null;

// ── Health + control server ───────────────────────────────────────────────────
// GET  /health      — liveness probe for Python API
// POST /send-reply  — Python sends HITL bot messages via this endpoint

const healthServer = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", wa_connected: isConnected, group_jid: GROUP_JID }));
    return;
  }

  if (req.method === "POST" && req.url === "/send-reply") {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", async () => {
      try {
        const { group_jid, text, quote_id } = JSON.parse(body);
        if (!activeSock || !isConnected) {
          res.writeHead(503, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "WA not connected" }));
          return;
        }
        let sent;
        if (quote_id) {
          // Reply quoting the original operator message
          const quotedStub = {
            key: { remoteJid: group_jid, id: quote_id, fromMe: false },
            message: { conversation: "…" },
          };
          sent = await activeSock.sendMessage(group_jid, { text }, { quoted: quotedStub });
        } else {
          sent = await activeSock.sendMessage(group_jid, { text });
        }
        const botMsgId = sent?.key?.id || null;
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ sent: true, bot_message_id: botMsgId }));
      } catch (err) {
        console.error("[Fleet WA Listener] /send-reply error:", err.message);
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
    return;
  }

  res.writeHead(404);
  res.end();
});

healthServer.listen(HEALTH_PORT, () => {
  console.log(`[Fleet WA Listener] Health endpoint: http://localhost:${HEALTH_PORT}/health`);
});

// ── Baileys connection ────────────────────────────────────────────────────────

async function startListener() {
  if (!fs.existsSync(SESSION_DIR)) {
    fs.mkdirSync(SESSION_DIR, { recursive: true });
  }

  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();

  console.log(`[Fleet WA Listener] Baileys version: ${version.join(".")}`);
  console.log(`[Fleet WA Listener] API: ${API_URL}`);
  console.log(`[Fleet WA Listener] Target group JID: ${GROUP_JID || "(not set — all groups)"}`);

  let retryCount = 0;

  function connect() {
    const sock = makeWASocket({
      version,
      auth:  state,
      logger: pino({ level: "silent" }),
      printQRInTerminal: false,
      generateHighQualityLinkPreview: false,
    });

    // Expose sock so /send-reply can use it
    activeSock = sock;

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
      if (qr) {
        console.log("\n[Fleet WA Listener] Scan QR code to authenticate:\n");
        qrcode.generate(qr, { small: true });
      }
      if (connection === "open") {
        retryCount   = 0;
        isConnected  = true;
        console.log("[Fleet WA Listener] ✓ Connected to WhatsApp");
      }
      if (connection === "close") {
        isConnected = false;
        activeSock  = null;
        const reason = lastDisconnect?.error?.output?.statusCode;
        const shouldReconnect = reason !== DisconnectReason.loggedOut;
        console.warn(`[Fleet WA Listener] Connection closed. Reason: ${reason}. Reconnect: ${shouldReconnect}`);
        if (shouldReconnect) {
          const delay = Math.min(1000 * Math.pow(2, retryCount++), 60000);
          console.log(`[Fleet WA Listener] Reconnecting in ${delay}ms…`);
          setTimeout(connect, delay);
        } else {
          console.error("[Fleet WA Listener] Logged out. Delete session/ and restart to re-authenticate.");
          process.exit(1);
        }
      }
    });

    sock.ev.on("messages.upsert", async ({ messages, type }) => {
      if (type !== "notify") return;

      for (const msg of messages) {
        const jid = msg.key.remoteJid || "";
        if (GROUP_JID && jid !== GROUP_JID) continue;
        if (!jid.endsWith("@g.us")) continue;

        // Skip messages sent by our own bot account
        if (msg.key.fromMe) continue;

        const text = (
          msg.message?.conversation ||
          msg.message?.extendedTextMessage?.text ||
          ""
        ).trim();
        if (!text) continue;

        const wa_message_id = msg.key.id || `${Date.now()}-${Math.random()}`;
        const sender_phone  = msg.key.participant || jid;
        const sender_name   = msg.pushName || null;
        const received_at   = new Date(
          msg.messageTimestamp ? Number(msg.messageTimestamp) * 1000 : Date.now()
        ).toISOString();
        const message_type  = detectMessageType(text);

        // Detect if this message is a reply to a previous message (e.g. a bot HITL question)
        const quoted_wa_message_id =
          msg.message?.extendedTextMessage?.contextInfo?.stanzaId || null;

        const logExtra = quoted_wa_message_id ? ` [reply to ${quoted_wa_message_id.slice(0, 8)}…]` : "";
        console.log(`[Fleet WA Listener] [${message_type}]${logExtra} ${sender_phone}: ${text.slice(0, 80)}`);

        // POST directly to Python pipeline
        const result = await postToAPI("/api/ingest/wa-message", {
          wa_message_id,
          sender_phone,
          sender_name,
          group_jid:             jid,
          raw_text:              text,
          received_at,
          message_type,
          quoted_wa_message_id,  // null when not a reply
        });

        if (result.status >= 200 && result.status < 300) {
          const parsed = (() => { try { return JSON.parse(result.body); } catch { return {}; } })();
          const routed = parsed.routed_as === "hitl_answer" ? " [HITL answer]" : "";
          console.log(`[Fleet WA Listener] ✓ processed (HTTP ${result.status})${routed}`);
        } else {
          console.error(`[Fleet WA Listener] ✗ API error (HTTP ${result.status}): ${result.body.slice(0, 200)}`);
        }
      }
    });
  }

  connect();
}

// ── Entry point ───────────────────────────────────────────────────────────────

startListener().catch((err) => {
  console.error("[Fleet WA Listener] Fatal error:", err);
  process.exit(1);
});
