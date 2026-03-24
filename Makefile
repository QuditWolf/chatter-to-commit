.PHONY: up down build logs shell-api shell-wa qr restart-wa

# Start everything (build if needed)
up:
	docker compose up -d --build

# Stop and remove containers (volumes preserved)
down:
	docker compose down

# Build images without starting
build:
	docker compose build

# Follow all logs
logs:
	docker compose logs -f

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

# Wipe WA session and re-authenticate
reset-wa-session:
	docker compose stop wa
	docker volume rm $$(docker compose config --volumes | grep wa_session) 2>/dev/null || true
	docker compose up -d wa
	docker compose logs -f wa

# Hard reset: wipe DB and WA session (DESTRUCTIVE)
reset-all:
	docker compose down -v
	docker compose up -d --build
