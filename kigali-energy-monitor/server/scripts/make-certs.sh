#!/usr/bin/env bash
# Make a private CA and a server certificate for the MQTT broker.
# Usage: ./scripts/make-certs.sh mqtt.example.rw
set -euo pipefail
HOST=${1:?broker hostname, e.g. mqtt.example.rw}
D=$(dirname "$0")/../mosquitto/certs
mkdir -p "$D" && cd "$D"
[[ -f ca.key ]] || openssl req -x509 -newkey rsa:4096 -nodes -days 3650 -keyout ca.key -out ca.crt -subj "/CN=Energy Monitor CA"
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=$HOST"
printf "subjectAltName=DNS:%s\n" "$HOST" > san.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -out server.crt -extfile san.ext
rm -f server.csr san.ext
chmod 644 server.crt ca.crt; chmod 600 server.key
sudo chown 1883:1883 server.key 2>/dev/null || chmod 644 server.key   # mosquitto runs as uid 1883
echo "Copy $(pwd)/ca.crt to each Pi as /etc/energy-gateway/ca.crt. Keep ca.key offline."
