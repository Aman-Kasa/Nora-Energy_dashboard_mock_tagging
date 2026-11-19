"""Forward buffered readings to the MQTT broker.

Rows are sent in batches as one JSON message. A batch is deleted from the
local buffer only after the broker returns PUBACK (QoS 1). If the broker
is unreachable, the batch simply stays in SQLite and is retried later.
The server side inserts idempotently, so a batch that is sent twice is harmless.
"""
import json
import logging
import ssl
import threading
import time

log = logging.getLogger("publisher")


class MqttPublisher:
    def __init__(self, cfg, gateway_id, site):
        import paho.mqtt.client as mqtt   # lazy import

        self.mqtt = mqtt
        self.cfg = cfg
        self.topic_base = f"{cfg.get('topic_prefix', 'kigali')}/{site}/{gateway_id}"
        self.connected = threading.Event()
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=gateway_id, clean_session=False)
        except AttributeError:          # paho-mqtt 1.x
            self.client = mqtt.Client(client_id=gateway_id, clean_session=False)
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password"))
        if cfg.get("tls", True):
            self.client.tls_set(ca_certs=cfg.get("ca_file"), cert_reqs=ssl.CERT_REQUIRED,
                                tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.will_set(f"{self.topic_base}/status", json.dumps({"online": False}), qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=2, max_delay=120)

    def _on_connect(self, client, userdata, flags, rc, *args):
        ok = (rc == 0) if isinstance(rc, int) else not rc.is_failure
        if ok:
            log.info("MQTT connected")
            self.connected.set()
            client.publish(f"{self.topic_base}/status", json.dumps({"online": True}), qos=1, retain=True)
        else:
            log.error("MQTT connect refused: %s", rc)

    def _on_disconnect(self, client, userdata, *args):
        log.warning("MQTT disconnected")
        self.connected.clear()

    def start(self):
        self.client.connect_async(self.cfg["host"], int(self.cfg.get("port", 8883)), keepalive=60)
        self.client.loop_start()

    def send(self, kind, payload, timeout=15):
        if not self.connected.is_set():
            return False
        topic = f"{self.topic_base}/{'readings' if kind == 'reading' else 'events'}"
        info = self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1)
        try:
            info.wait_for_publish(timeout=timeout)
        except (RuntimeError, ValueError):
            return False
        return info.is_published()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


class PrintPublisher:
    """For bench tests: prints a one-line summary instead of sending."""

    def __init__(self, *a, **k):
        self.sent = []

    def start(self):
        pass

    def send(self, kind, payload, timeout=0):
        self.sent.append((kind, payload))
        n = len(payload.get("items", []))
        log.info("[dry-run] %s batch seq=%s items=%d", kind, payload.get("seq"), n)
        return True

    def stop(self):
        pass


def forward_loop(buffer, publisher, gateway_id, site, stop_event, batch=500, idle_s=2.0):
    """Drain the outbox forever (run in a thread)."""
    while not stop_event.is_set():
        sent_any = False
        for kind in ("event", "reading"):
            rows = buffer.peek(kind, batch)
            if not rows:
                continue
            payload = {"gw": gateway_id, "site": site, "seq": buffer.next_seq(),
                       "sent": time.time(), "items": [body for _id, body in rows]}
            if publisher.send(kind, payload):
                buffer.ack([rid for rid, _ in rows])
                sent_any = True
            else:
                break
        if not sent_any:
            stop_event.wait(idle_s)
