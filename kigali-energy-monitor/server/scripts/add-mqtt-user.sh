#!/usr/bin/env bash
# Add or update an MQTT user (a gateway id, or 'ingest').
# Usage: ./scripts/add-mqtt-user.sh pi5-mall-01 'long-random-password'
set -euo pipefail
U=${1:?username}; P=${2:?password}
cd "$(dirname "$0")/.."
touch mosquitto/config/passwd
docker run --rm -v "$(pwd)/mosquitto/config:/c" eclipse-mosquitto:2 mosquitto_passwd -b /c/passwd "$U" "$P"
docker compose kill -s HUP mosquitto 2>/dev/null || true
echo "user $U set"
