# Deployment

## Option A — Docker (recommended)

### Prerequisites
- Docker ≥ 24 and Docker Compose v2
- An OpenAI-compatible LLM server reachable from the container

### Steps

```bash
# 1. Clone and enter the repo
git clone <repo> && cd fleet-tracker

# 2. Create your .env
cp .env.example .env

# 3. Edit .env — minimum required:
#    HOST_PORT=8080
#    FLEET_LLM_BASE_URL=http://host.docker.internal:8001/v1
#
#    Leave WA_GROUP_JID blank for the first run

# 4. Build images and start
make up
# or: docker compose up -d --build

# 5. Follow logs to see startup
docker compose logs -f

# 6. Scan WA QR (first run only)
make qr
# A QR code prints in the terminal — scan with WhatsApp (Linked Devices)

# 7. Discover your group JID
#    Send any message in the WA group, then:
docker compose logs api | grep group_jid
#    Example output: "group_jid": "120363012345678901@g.us"
#    Add to .env: WA_GROUP_JID=120363012345678901@g.us

# 8. Restart WA listener to pick up the JID
make restart-wa

# 9. Open the dashboard
open http://localhost:8080   # replace 8080 with your HOST_PORT
```

### Persistence

| Data | Location | Survives `docker compose down`? |
|------|----------|--------------------------------|
| Database | Docker volume `fleet_db` | ✅ Yes |
| WA session | Docker volume `wa_session` | ✅ Yes |

`docker compose down -v` removes volumes (destructive).

### Makefile reference

```bash
make up              # build + start all services
make down            # stop (keeps volumes)
make build           # rebuild images without starting
make logs            # follow all logs
make qr              # follow WA container logs (shows QR)
make shell-api       # bash inside api container
make shell-wa        # sh inside wa container
make restart-wa      # restart only the WA listener
make reset-wa-session  # wipe WA session, re-scan QR
make reset-all       # destroy everything including volumes (DESTRUCTIVE)
```

### Updating

```bash
git pull
make up   # rebuilds changed images, rolls over containers
```

---

## Option B — Bare Metal (PM2)

Use this when Docker is not available or you want direct process control.

### Prerequisites

```bash
# Python 3.11+
pip install -r requirements.txt

# Node.js 18+
cd fleet_pipeline/wa_listener
npm install
```

### First run

```bash
# 1. Initialise database
python -m fleet_pipeline.db.migrate

# 2. Copy and edit env
cp .env.example .env
# Set FLEET_LLM_BASE_URL and WA_GROUP_JID

# 3. Start with PM2
pm2 start ecosystem.config.js
pm2 save
pm2 startup   # auto-start on reboot
```

### Useful PM2 commands

```bash
pm2 status               # show all processes
pm2 logs fleet-api       # API logs
pm2 logs fleet-wa        # WA listener logs (shows QR on first run)
pm2 restart fleet-wa     # restart WA listener
pm2 stop all             # stop everything
```

---

## nginx Configuration

### Behind your own nginx (no Docker nginx service)

Remove the `nginx` service from `docker-compose.yml` (or just don't use it). Proxy directly to the API container or PM2 process:

```nginx
server {
    listen 443 ssl;
    server_name yourhost.example.com;

    # SSL config here...

    # WebSocket — must come before catch-all
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        proxy_read_timeout 86400s;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host               $host;
        proxy_set_header X-Real-IP          $remote_addr;
        proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto  $scheme;
        proxy_read_timeout    300s;   # LLM pipeline takes 15-120s
        proxy_send_timeout    300s;
        client_max_body_size  10m;
    }
}
```

Replace `127.0.0.1:8000` with the Docker-mapped HOST_PORT if using Docker without Docker nginx.

---

## Environment Variables Reference

### Host / network

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST_PORT` | `8080` | External port nginx binds to on the host |

### LLM

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_LLM_BASE_URL` | — | OpenAI-compatible endpoint, e.g. `http://host.docker.internal:8001/v1` |
| `FLEET_LLM_API_KEY` | `EMPTY` | API key. Use `EMPTY` for local models (vllm, Ollama) |
| `FLEET_MODEL` | `Qwen/Qwen2.5-7B-Instruct-AWQ` | Model name sent in the API request |
| `FLEET_LLM_MAX_TOKENS` | `2048` | Max tokens in LLM response. Minimum 1500 recommended |
| `FLEET_LLM_TEMPERATURE` | `0.0` | Sampling temperature. 0 = deterministic |
| `FLEET_LLM_MOCK` | `false` | Skip LLM entirely; use simple rule-based fallback (dev/testing) |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_DB_PATH` | `fleet_pipeline/data/fleet.db` | Absolute path to SQLite file. In Docker: `/data/fleet.db` (on volume) |

### API server

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_API_HOST` | `0.0.0.0` | Uvicorn bind host |
| `FLEET_API_PORT` | `8000` | Uvicorn bind port (internal, not exposed directly) |

### WhatsApp

| Variable | Default | Description |
|----------|---------|-------------|
| `WA_GROUP_JID` | — | WA group JID (`120363XXXXXX@g.us`). Leave blank to listen to all groups |
| `WA_LISTENER_URL` | `http://localhost:3001` | URL the Python API uses to call `/send-reply`. In Docker: `http://wa:3001` (set automatically) |
| `WA_HEALTH_PORT` | `3001` | Port the Node.js health+control server listens on |
| `WA_SESSION_DIR` | `./session` | Path for Baileys auth session. In Docker: `/session` (on volume) |
| `LOG_LEVEL` | `info` | Pino log level for WA listener (`trace/debug/info/warn/error`) |

---

## LLM Server Setup

The pipeline needs an OpenAI-compatible inference server. Any of these work:

### vllm (recommended for GPU)

```bash
pip install vllm
vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ \
  --port 8001 \
  --tensor-parallel-size 1

# Set in .env:
FLEET_LLM_BASE_URL=http://host.docker.internal:8001/v1
FLEET_MODEL=Qwen/Qwen2.5-7B-Instruct-AWQ
```

### Ollama (CPU / Apple Silicon)

```bash
brew install ollama
ollama run qwen2.5:7b-instruct

# Set in .env:
FLEET_LLM_BASE_URL=http://host.docker.internal:11434/v1
FLEET_MODEL=qwen2.5:7b-instruct
FLEET_LLM_API_KEY=ollama
```

### Remote LLM server

```bash
FLEET_LLM_BASE_URL=http://192.168.1.50:8001/v1
```

From inside Docker containers, `localhost` refers to the container itself, not the host. Use:
- `host.docker.internal` on Mac / Windows Docker Desktop
- The host's LAN IP (e.g. `192.168.1.x`) on Linux

---

## WA Group JID — How to Find It

1. Start the system without `WA_GROUP_JID` set
2. Send any message to your target WA group
3. Look at the Node.js logs:
   ```
   [fleet_event] +919876543210@s.whatsapp.net: test
   ```
   The `remoteJid` of that message is your group JID (ends in `@g.us`)
4. Alternatively check the Python API logs or the `wa_messages` table:
   ```sql
   SELECT DISTINCT group_jid FROM wa_messages LIMIT 5;
   ```

---

## Seeding Trucks and Sites

After first run, populate the registry:

```bash
# From inside the api container or on bare metal:
python -m fleet_pipeline.db.seed_data

# Or use the Admin tab in the dashboard → Truck Registry / Site Registry
# to add trucks and sites manually
```

---

## Resetting Data

```bash
# Keep WA session, wipe DB only
docker compose exec api python -c "
import sqlite3, os
from fleet_pipeline.config import DB_PATH
os.remove(DB_PATH)
from fleet_pipeline.db.database import init_db
init_db(DB_PATH)
print('DB reset')
"
docker compose restart api

# Full reset (wipes everything including WA session)
make reset-all
```
