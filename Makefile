.PHONY: up down build logs logs-tail logs-history shell-api shell-wa qr restart-wa find-groups reset-wa-session

# Start everything (build if needed)
up:
	docker compose up -d --build

# Stop and remove containers (volumes preserved)
down:
	docker compose down

# Build images without starting
build:
	docker compose build

# Follow all logs (docker container stdout/stderr)
logs:
	docker compose logs -f

# Live tail of all log files combined (api.log + wa.log), prefixed with filename
logs-tail:
	tail -f logs/*.log

# Historical view: merge all log files sorted by timestamp, pipe to less
# sort -m = merge-sort (files must be internally sorted, which log files always are)
logs-history:
	sort -m logs/*.log | less

# Show WA QR code (needed on first run or after session reset)
qr:
	docker compose logs -f wa

# Shell into API container
shell-api:
	docker compose exec api bash

# Shell into WA container
shell-wa:
	docker compose exec wa sh

# Restart only the WA listener (e.g. after session issues)
restart-wa:
	docker compose restart wa

# Find and list groups the bot is part of (shows JIDs)
find-groups:
	docker compose run --rm wa node index.js --find-groups

# Reset WA session and re-authenticate
reset-wa-session:
	docker compose stop wa
	docker volume rm 02_truck_fleet_wa_session 2>/dev/null || true
	docker compose up -d wa
	docker compose logs -f wa

# Hard reset: wipe DB and WA session (DESTRUCTIVE)
reset-all:
	docker compose down -v
	docker compose up -d --build
