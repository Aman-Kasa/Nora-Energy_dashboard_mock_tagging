# Kigali Building Energy Monitor

This project measures the electricity used by every shop in a multi-tenant building, using RS485 meters and a Raspberry Pi 5.
It knows at every moment whether the building runs on **grid (REG/EUCL)** or **generator**. Every kWh is billed at the right price.

```
meters + generator + ATS ──RS485/Modbus──► Raspberry Pi 5 ──MQTT/TLS──► Mosquitto ► ingest ► TimescaleDB ► Grafana
                         └─ATS contact─► GPIO17                                                     └► billing (RWF, CSV)
```

## What is in the repo

| Path | What it does |
| --- | --- |
| `gateway/main.py` | The Pi service: polls all buses in parallel, detects GRID / GENERATOR / NONE, tags readings, buffers, forwards |
| `gateway/registers.py` | Register maps: Eastron SDM120, SDM630/SDM72D, Deep Sea GenComm (DSE) |
| `gateway/modbus_bus.py` | Read-only Modbus RTU access (function codes 03/04 only), block reads, retries |
| `gateway/source.py` | Supply detection from the ATS contact, with a fallback to the meters, plus debouncing |
| `gateway/buffer.py` | SQLite outbox (store-and-forward) and the kWh-delta bookkeeping |
| `gateway/publisher.py` | MQTT publisher (TLS, QoS 1, deletes local rows only after PUBACK) |
| `gateway/simulator.py` | A simulated building with shops, outages, generator, fuel and ATS, so the stack runs without hardware |
| `gateway/scan_bus.py` | Commissioning tool: finds which Modbus addresses answer on a bus |
| `gateway/install.sh`, `systemd/` | Pi installation, service user, watchdog and firewall |
| `server/docker-compose.yml` | Mosquitto, TimescaleDB, ingest, Grafana and the billing job |
| `server/db/` | Database schema (hypertable, hourly continuous aggregate, outage view, bills) |
| `server/app/ingest.py` | MQTT to database writer (idempotent) |
| `server/app/billing.py`, `billing_core.py`, `tariffs.yaml` | Monthly bills, split by grid and generator, in RWF |
| `server/scripts/build_dashboard.py` | Generates the Grafana dashboard (12 panels) and alert rules (5) |
| `tests/` | Unit tests, plus an end-to-end test that checks the grid/generator energy split against the simulator's ground truth |

## 1. Try it with no hardware (laptop or Pi)

```bash
pip install pyyaml
cd gateway
# real-time-ish: 1 real second = 1 simulated minute, print batches instead of sending them
python3 main.py --config config.example.yaml --simulate --speed 60 --dry-run --buffer /tmp/gw.db
# run the tests (from the repo root)
cd .. && python3 -m unittest discover -s tests -v
```

To feed the simulator into the real server, drop `--dry-run` and point `mqtt:` in a copy of the config at your broker.

## 2. Server

```bash
cd server
cp .env.example .env                                    # set strong passwords
./scripts/make-certs.sh mqtt.yourdomain.rw              # private CA + broker certificate
source .env
./scripts/add-mqtt-user.sh ingest "$MQTT_INGEST_PASSWORD"
./scripts/add-mqtt-user.sh pi5-mall-01 "$(openssl rand -base64 24)"   # note it for the Pi
docker compose up -d
# Grafana on :3000 (admin / GRAFANA_ADMIN_PASSWORD). Reach it over the VPN or behind HTTPS.
```

## 3. Raspberry Pi 5 gateway

1. Install Raspberry Pi OS Lite 64-bit (Bookworm). Set up SSH keys, then join Tailscale (`curl -fsSL https://tailscale.com/install.sh | sh`).
2. Connect one **isolated** USB-RS485 adapter per bus. Wire the ATS auxiliary contact through an optocoupler to GPIO17 and GND.
3. `cd gateway && sudo ./install.sh`
4. Find the adapters with `ls -l /dev/serial/by-id/`, then edit `/etc/energy-gateway/config.yaml`.
5. Check each bus: `sudo -u energy /opt/energy-gateway/venv/bin/python /opt/energy-gateway/scan_bus.py --port /dev/serial/by-id/...`
6. Put the MQTT password in `/etc/energy-gateway/mqtt.pass` and the CA in `/etc/energy-gateway/ca.crt`.
7. `sudo systemctl start energy-gateway && journalctl -u energy-gateway -f`

## 4. Commissioning checklist

- [ ] Every meter has the same baud rate and parity (9600 8N1) and a unique address on its bus, and is labelled
- [ ] 120 Ω termination at both ends of each bus only; shield earthed at one end
- [ ] `scan_bus.py` finds every expected address
- [ ] Toggle the ATS (or wait for an outage): the dashboard shows GRID, then NONE, then GENERATOR, then GRID, with the right times
- [ ] Deep Sea registers checked against the GenComm document for the **exact** controller model (see `registers.py`)
- [ ] Main meter kWh compared with the EUCL meter over two weeks (within 1–2%)
- [ ] Sum of the shop meters is lower than main + generator; the gap (common areas) looks plausible
- [ ] Pull the Pi's network cable for an hour, reconnect, and confirm no gap in the data

## 5. Monthly bills

```bash
cd server
docker compose run --rm billing --site kigali-mall --month 2026-11 --run-by aman --dry-run
# with the real EUCL bill and the fuel receipts (most accurate):
docker compose run --rm billing --site kigali-mall --month 2026-11 \
    --eucl-bill 5400000 --fuel-litres 820 --run-by aman
```

The CSV lands in `server/bills/`. Rules:

- **Grid rate** is the EUCL bill ÷ main meter kWh. With no bill entered, it falls back to the RURA non-residential blocks in `tariffs.yaml` (355 / 376 RWF/kWh, plus 18% VAT, from 1 Oct 2025).
- **Generator rate** is (litres × diesel price + run hours × maintenance) ÷ generator kWh. Litres come from receipts, or else from the fuel level before and after each run.
- **Common areas and losses** are (main + generator) − Σ shops, split by floor area (or by kWh, or equally).

`gen_maintenance_rwf_per_hour` in `tariffs.yaml` is a placeholder. Set it with the landlord, and check the diesel price before each run.

## How grid vs generator energy is split

Every meter's cumulative kWh is read every poll (10 s). The gateway stores the difference since the last reading (`d_kwh`) and tags it with the supply that was on during that interval. A difference seen right after a blackout is given to the supply that was on before it, because no energy flows while the building is dark. At most one 10-second interval per switch-over can land on the wrong side.

Over a simulated month with 87 outages, the gateway's split matched the simulator's ground truth within 0.3%.

## Security notes

- Modbus RTU has no authentication. The gateway is read-only by design and holds no write function.
- The Pi accepts no inbound traffic except SSH over the VPN. It sends only outbound MQTT over TLS on 8883.
- Each gateway has its own MQTT user, and the ACL lets it publish only under its own id.
- Grafana reads the database through a read-only role. Billing runs are logged with who ran them.

## Known limits

- The Deep Sea GenComm addresses differ between controller families. Verify them on site. ComAp controllers need their own map in `registers.py`.
- When the ATS input is missing, a silent main meter is read as "grid down". That only works if the main meter is on the same bus as other devices that keep answering.
