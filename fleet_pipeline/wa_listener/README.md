# Fleet WA Listener

Real-time WhatsApp ingestion using [Baileys](https://github.com/WhiskeySockets/Baileys).

## Setup

```bash
cd fleet_pipeline/wa_listener
npm install
cp .env.example .env
# Edit .env — set WA_GROUP_JID and FLEET_DB_PATH
```

## First run (QR auth)

```bash
node index.js
```

Scan the QR code with WhatsApp on the target device. Session is saved to `./session/` and reused on subsequent starts.

## Finding your group JID

1. Start listener without `WA_GROUP_JID` set.
2. Send a message to the target group.
3. The listener logs the JID — copy it to `.env`.

## Running with PM2 (production)

```bash
pm2 start ecosystem.config.js
pm2 save
pm2 startup
```

## What it does

1. Connects to WhatsApp via the Web protocol (no Meta Business API needed).
2. Listens only to the configured group JID.
3. For each new message:
   - Extracts sender phone, timestamp, text.
   - Detects if it's a shift signal or fleet event.
   - Inserts into `wa_messages` table in fleet.db.
4. The Python pipeline reads `wa_messages` and processes fleet events.

## Shift signal detection

Messages matching these patterns are stored as `message_type = "shift_signal"` and not parsed as fleet events:
- `shift start`, `shift end`, `shift started`, etc.
- `s1`, `s2`, `s3`
- `shift 1`, `shift 2`, `shift 3`
