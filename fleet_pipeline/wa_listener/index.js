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

const GROUP_JID         = process.env.WA_GROUP_JID         || "";
const CONTROL_GROUP_JID = process.env.WA_CONTROL_GROUP_JID || "";
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

// ── Bot-sent message ID tracking ──────────────────────────────────────────────
// When the Python API sends a message via /send-reply or /send-message, we store
// the returned bot_message_id here. When Baileys echoes that message back to us
// (fromMe=true), we skip it — it's our own send, not a human typing.
// IDs expire after 2 hours to prevent memory growth.

const _botSentIds = new Map(); // id → timestamp
const BOT_SENT_TTL = 2 * 60 * 60 * 1000; // 2 hours

function trackBotSentId(id) {
  if (!id) return;
  _botSentIds.set(id, Date.now());
  // Lazy cleanup: remove entries older than TTL
  for (const [k, ts] of _botSentIds) {
    if (Date.now() - ts > BOT_SENT_TTL) _botSentIds.delete(k);
  }
}

function isBotSentId(id) {
  return _botSentIds.has(id);
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
        trackBotSentId(botMsgId);
        console.log(`[Fleet WA Listener] [SENT] /send-reply → ${group_jid}: ${text.slice(0, 80).replace(/\n/g, " ")}`);
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

  // POST /send-message — plain message to group (no quote), used for summaries
  if (req.method === "POST" && req.url === "/send-message") {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", async () => {
      try {
        const { group_jid, text } = JSON.parse(body);
        if (!activeSock || !isConnected) {
          res.writeHead(503, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "WA not connected" }));
          return;
        }
        const sent = await activeSock.sendMessage(group_jid, { text });
        const botMsgId = sent?.key?.id || null;
        trackBotSentId(botMsgId);
        console.log(`[Fleet WA Listener] [SENT] /send-message → ${group_jid}: ${text.slice(0, 80).replace(/\n/g, " ")}`);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ sent: true, bot_message_id: botMsgId }));
      } catch (err) {
        console.error("[Fleet WA Listener] /send-message error:", err.message);
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

// ── Group JID finder ──────────────────────────────────────────────────────────

async function findGroups() {
  if (!fs.existsSync(SESSION_DIR)) {
    fs.mkdirSync(SESSION_DIR, { recursive: true });
  }

  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();

  console.log(`\n=== Fleet WA Listener — Group Finder ===\n`);

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: "silent" }),
    printQRInTerminal: false,
  });

  sock.ev.on("creds.update", saveCreds);

  return new Promise((resolve) => {
    sock.ev.on("connection.update", async ({ connection, qr }) => {
      if (qr) {
        console.log("\nScan QR to authenticate:\n");
        qrcode.generate(qr, { small: true });
      }
      if (connection === "open") {
        console.log("✓ Connected! Fetching groups...\n");

        try {
          // groupFetchAllParticipating can hang — race against a 10s timeout
          const groups = await Promise.race([
            sock.groupFetchAllParticipating(),
            new Promise((_, reject) =>
              setTimeout(() => reject(new Error("Timed out fetching groups (10s)")), 10000)
            ),
          ]);

          const groupList = Object.values(groups).sort((a, b) =>
            (a.subject || "").localeCompare(b.subject || "")
          );

          if (groupList.length === 0) {
            console.log("No groups found (account may not be in any groups).");
          } else {
            console.log("Your groups:\n");
            groupList.forEach((g, i) => {
              const members = g.participants?.length || 0;
              console.log(`  ${i + 1}. ${g.subject || "Unnamed group"}`);
              console.log(`     JID: ${g.id}`);
              console.log(`     Members: ${members}\n`);
            });
          }

          console.log("Add the relevant JIDs to your .env:");
          console.log(`  WA_GROUP_JID=<fleet group JID>         # read-only, truck events`);
          console.log(`  WA_CONTROL_GROUP_JID=<control JID>     # bot replies, shift signals\n`);
        } catch (err) {
          console.error("Error fetching groups:", err.message);
          console.log("\nTip: if this keeps timing out, try sending any message to");
          console.log("the target group — its JID will appear in the listener logs.\n");
        }

        // Close connection without logout so the session is preserved for next run
        try { sock.end(); } catch (_) {}
        resolve();
      }
      if (connection === "close") {
        resolve();
      }
    });
  }).finally(() => {
    // Ensure the process exits even if something keeps the event loop alive
    setTimeout(() => process.exit(0), 500);
  });
}

// ── Baileys connection ────────────────────────────────────────────────────────

async function startListener() {
  if (!fs.existsSync(SESSION_DIR)) {
    fs.mkdirSync(SESSION_DIR, { recursive: true });
  }

  const { version } = await fetchLatestBaileysVersion();
  console.log(`[Fleet WA Listener] Baileys version: ${version.join(".")}`);
  console.log(`[Fleet WA Listener] API: ${API_URL}`);
  console.log(`[Fleet WA Listener] Fleet group JID (read-only): ${GROUP_JID || "(not set)"}`);
  console.log(`[Fleet WA Listener] Control group JID (bot replies): ${CONTROL_GROUP_JID || "(not set — same as fleet)"}`);;

  let retryCount = 0;
  let isFirstConnection = true;

  function connect() {
    // Recreate auth state on each connect attempt (avoids stale state issues)
    useMultiFileAuthState(SESSION_DIR).then(({ state, saveCreds }) => {
      const sock = makeWASocket({
        version,
        auth:  state,
        logger: pino({ level: "silent" }),
        printQRInTerminal: false,
        generateHighQualityLinkPreview: false,
      });

      activeSock = sock;

      sock.ev.on("creds.update", saveCreds);

      sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
        if (qr) {
          console.log("\n[Fleet WA Listener] Scan QR code to authenticate:\n");
          qrcode.generate(qr, { small: true });
          isFirstConnection = false;
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
          const isLoggedOut = reason === DisconnectReason.loggedOut;
          
          console.warn(`[Fleet WA Listener] Connection closed. Reason: ${reason}`);
          
          if (isLoggedOut) {
            console.error("[Fleet WA Listener] ✗ Session invalidated by WhatsApp. Clearing session and restarting...");
            // Clear session files
            try {
              const files = fs.readdirSync(SESSION_DIR);
              for (const file of files) {
                fs.unlinkSync(path.join(SESSION_DIR, file));
              }
              console.log("[Fleet WA Listener] Session cleared. Will show new QR code.");
            } catch (e) {
              console.error("[Fleet WA Listener] Failed to clear session:", e.message);
            }
            // Restart fresh (no reconnect delay)
            setTimeout(connect, 1000);
          } else {
            // Temporary disconnect - reconnect with backoff
            const shouldReconnect = reason !== DisconnectReason.loggedOut;
            if (shouldReconnect) {
              const delay = Math.min(1000 * Math.pow(2, retryCount++), 60000);
              console.log(`[Fleet WA Listener] Reconnecting in ${delay}ms…`);
              setTimeout(connect, delay);
            } else {
              console.error("[Fleet WA Listener] Logged out. Delete session/ and restart to re-authenticate.");
              process.exit(1);
            }
          }
        }
      });

      sock.ev.on("messages.update", (updates) => {
        for (const update of updates) {
          // Recalled/deleted message: update.update.message is null
          if (!update.update || update.update.message !== null) continue;
          const jid = update.key?.remoteJid || "";
          if (!jid.endsWith("@g.us")) continue;
          const knownGroups = [GROUP_JID, CONTROL_GROUP_JID].filter(Boolean);
          if (knownGroups.length > 0 && !knownGroups.includes(jid)) continue;

          const wa_message_id = update.key.id;
          const deleted_by = update.key.participant || jid;
          console.log(`[Fleet WA Listener] [DELETE] Message recalled: ${wa_message_id} by ${deleted_by}`);

          postToAPI("/api/ingest/wa-message-deleted", {
            wa_message_id,
            group_jid: jid,
            deleted_by,
          }).then(r => {
            if (r.status >= 200 && r.status < 300) {
              const parsed = (() => { try { return JSON.parse(r.body); } catch { return {}; } })();
              console.log(`[Fleet WA Listener] [DELETE] Marked ${parsed.events_deleted || 0} event(s) as deleted`);
            }
          });
        }
      });

      sock.ev.on("messages.upsert", async ({ messages, type }) => {
        if (type !== "notify") return;

        for (const msg of messages) {
          const jid = msg.key.remoteJid || "";
          if (!jid.endsWith("@g.us")) continue;
          // Accept messages from fleet group and/or control group.
          // If neither is configured, accept all groups.
          const knownGroups = [GROUP_JID, CONTROL_GROUP_JID].filter(Boolean);
          if (knownGroups.length > 0 && !knownGroups.includes(jid)) continue;

          // For the fleet group: always skip fromMe (bot never needs to read its own echoes there).
          // For the control group: allow fromMe unless it's a message we sent (tracked in _botSentIds).
          // This lets the operator send "summary" from the same phone the bot runs on.
          if (msg.key.fromMe) {
            const isControl = CONTROL_GROUP_JID && jid === CONTROL_GROUP_JID;
            if (!isControl || isBotSentId(msg.key.id)) continue;
          }

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
          // Determine which group this message came from
          const source_group = (CONTROL_GROUP_JID && jid === CONTROL_GROUP_JID)
            ? "control"
            : "fleet";

          // For the fleet group: only fleet_event messages (no shift signals detected here)
          // For the control group: shift signals, HITL answers, summary requests
          const message_type = (source_group === "fleet")
            ? "fleet_event"
            : detectMessageType(text);

          // Detect if this message is a reply to a previous message (e.g. a bot HITL question)
          const quoted_wa_message_id =
            msg.message?.extendedTextMessage?.contextInfo?.stanzaId || null;

          const logExtra = quoted_wa_message_id ? ` [reply to ${quoted_wa_message_id.slice(0, 8)}…]` : "";
          console.log(`[Fleet WA Listener] [${source_group}][${message_type}]${logExtra} ${sender_phone}: ${text.slice(0, 80)}`);

          // POST directly to Python pipeline
          const result = await postToAPI("/api/ingest/wa-message", {
            wa_message_id,
            sender_phone,
            sender_name,
            group_jid:             jid,
            raw_text:              text,
            received_at,
            message_type,
            source_group,          // "fleet" | "control"
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
    }).catch((err) => {
      console.error("[Fleet WA Listener] Auth state error:", err.message);
      setTimeout(connect, 5000);
    });
  }

  connect();
}

// ── Entry point ────────────────────────────────────────────────────────────────

const args = process.argv.slice(2);

if (args.includes("--find-groups")) {
  findGroups().catch((err) => {
    console.error("[Fleet WA Listener] Fatal error:", err);
    process.exit(1);
  });
} else if (args.includes("--reset-session")) {
  console.log("[Fleet WA Listener] Clearing session directory...");
  try {
    if (fs.existsSync(SESSION_DIR)) {
      const files = fs.readdirSync(SESSION_DIR);
      for (const file of files) {
        fs.unlinkSync(path.join(SESSION_DIR, file));
      }
    }
    console.log("[Fleet WA Listener] Session cleared. Run without --reset-session to re-authenticate.");
  } catch (e) {
    console.error("[Fleet WA Listener] Failed to clear session:", e.message);
    process.exit(1);
  }
} else {
  startListener().catch((err) => {
    console.error("[Fleet WA Listener] Fatal error:", err);
    process.exit(1);
  });
}

