"""MQTT -> TimescaleDB ingest service.

Subscribes to kigali/+/+/readings and kigali/+/+/events, and writes each batch
in one transaction. Inserts use ON CONFLICT DO NOTHING, so a batch that the
gateway re-sends (after a lost PUBACK) is stored only once.
"""
import json
import logging
import os
import signal
import ssl
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import psycopg

log = logging.getLogger("ingest")

DSN = os.environ.get("DATABASE_URL", "postgresql://energy:energy@db:5432/energy")
PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "kigali")

CORE = ("p_kw", "v", "i", "pf", "hz", "kwh")


def ts(epoch):
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc)


def reading_row(site, item):
    m = dict(item.get("m") or {})
    v = m.pop("v", None)
    if v is None:
        v = m.get("v_l1")
    i = m.pop("i", None)
    if i is None and "i_l1" in m:
        i = round(sum(m.get(k, 0) or 0 for k in ("i_l1", "i_l2", "i_l3")) / 3, 3)
    p_kw = m.pop("p_kw", None)
    if p_kw is None:
        p_kw = m.get("gen_p_kw")
    return (ts(item["ts"]), site, item["dev"], item.get("src", "GRID"), p_kw, v, i,
            m.pop("pf", None), m.pop("hz", None), m.pop("kwh", None), item.get("d_kwh"),
            json.dumps(m) if m else None)


class Ingest:
    def __init__(self):
        self.conn = psycopg.connect(DSN, autocommit=True)

    def handle(self, topic, payload):
        parts = topic.split("/")
        if len(parts) != 4:
            return
        _prefix, site, gateway, kind = parts
        if kind == "status":
            return
        body = json.loads(payload)
        items = body.get("items", [])
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO batches(gateway, seq, kind, items) VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT DO NOTHING", (gateway, body.get("seq"), kind, len(items)))
                if kind == "readings":
                    cur.executemany(
                        "INSERT INTO readings(ts, site, device_id, source, p_kw, v, i, pf, hz, kwh, d_kwh, m) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        [reading_row(site, it) for it in items])
                elif kind == "events":
                    for ev in items:
                        data = {k: v for k, v in ev.items() if k not in ("ts", "type")}
                        cur.execute("INSERT INTO events(ts, site, gateway, type, data) VALUES (%s,%s,%s,%s,%s) "
                                    "ON CONFLICT DO NOTHING",
                                    (ts(ev["ts"]), site, gateway, ev["type"], json.dumps(data)))
                        if ev["type"] == "devices":
                            self.upsert_devices(cur, site, ev.get("devices", []))
        log.info("%s/%s seq=%s: %d %s", site, gateway, body.get("seq"), len(items), kind)

    @staticmethod
    def upsert_devices(cur, site, devices):
        for d in devices:
            cur.execute(
                """INSERT INTO devices(device_id, site, role, name, floor, area_m2, contracted_kw, model, unit, tank_litres)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (device_id) DO UPDATE SET
                     site=excluded.site, role=excluded.role, model=excluded.model, unit=excluded.unit,
                     name=coalesce(devices.name, excluded.name),
                     floor=coalesce(devices.floor, excluded.floor),
                     area_m2=coalesce(devices.area_m2, excluded.area_m2),
                     contracted_kw=coalesce(devices.contracted_kw, excluded.contracted_kw),
                     tank_litres=coalesce(devices.tank_litres, excluded.tank_litres),
                     updated_at=now()""",
                (d["id"], site, d["role"], d.get("name"), d.get("floor"), d.get("area_m2"),
                 d.get("contracted_kw"), d.get("model"), d.get("unit"), d.get("tank_litres")))


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for attempt in range(30):
        try:
            ing = Ingest()
            break
        except psycopg.OperationalError as exc:
            log.warning("database not ready (%s), retrying", exc)
            time.sleep(2)
    else:
        raise SystemExit("database unreachable")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ingest", clean_session=False)
    client.username_pw_set(os.environ["MQTT_USER"], os.environ["MQTT_PASSWORD"])
    if os.environ.get("MQTT_TLS", "false").lower() == "true":
        client.tls_set(ca_certs=os.environ.get("MQTT_CA"), cert_reqs=ssl.CERT_REQUIRED)

    def on_connect(c, _u, _f, rc, _p=None):
        log.info("MQTT connected rc=%s", rc)
        c.subscribe([(f"{PREFIX}/+/+/readings", 1), (f"{PREFIX}/+/+/events", 1)])

    def on_message(_c, _u, msg):
        try:
            ing.handle(msg.topic, msg.payload)
        except Exception:
            log.exception("failed to store message on %s", msg.topic)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(os.environ.get("MQTT_HOST", "mosquitto"), int(os.environ.get("MQTT_PORT", 1883)))
    signal.signal(signal.SIGTERM, lambda *_: client.disconnect())
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
