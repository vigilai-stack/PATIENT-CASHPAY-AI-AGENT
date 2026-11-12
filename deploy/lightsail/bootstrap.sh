#!/usr/bin/env bash
# Runs ON the Lightsail server. It does not create any AWS resources.
set -euo pipefail
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo "Create .env from deploy/lightsail/lightsail.env.example first"; exit 1; }
grep -q '^SESSION_SECRET=.\+' .env || { echo "Set SESSION_SECRET in .env (e.g. openssl rand -hex 32)"; exit 1; }
docker compose -f docker-compose.yml -f deploy/lightsail/docker-compose.lightsail.yml up -d --build
docker compose ps
