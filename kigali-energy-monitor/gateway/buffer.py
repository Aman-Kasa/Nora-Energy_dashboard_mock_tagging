"""Local store-and-forward buffer (SQLite).

Every reading is written here first. The publisher sends rows in order and
deletes them only after the broker has acknowledged them, so an internet or
server outage of hours or days loses nothing (limited only by disk space).

The buffer also remembers the last cumulative kWh of each device, so the
gateway can compute an energy delta per interval and tag it with the source
(grid or generator) that supplied that interval, even across reboots.
"""
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,              -- 'reading' | 'event'
    body    TEXT NOT NULL               -- JSON
);
CREATE TABLE IF NOT EXISTS last_energy (
    device_id TEXT PRIMARY KEY,
    kwh       REAL NOT NULL,
    ts        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Buffer:
    def __init__(self, path, max_rows=20_000_000):
        self.path = path
        self.max_rows = max_rows
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # ---- outbox -------------------------------------------------------
    def put(self, kind, body):
        with self.lock:
            self.db.execute("INSERT INTO outbox(kind, body) VALUES (?, ?)", (kind, json.dumps(body, separators=(",", ":"))))

    def put_many(self, kind, bodies):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT INTO outbox(kind, body) VALUES (?, ?)",
                                [(kind, json.dumps(b, separators=(",", ":"))) for b in bodies])
            self.db.execute("COMMIT")

    def peek(self, kind, limit=500):
        with self.lock:
            rows = self.db.execute("SELECT id, body FROM outbox WHERE kind=? ORDER BY id LIMIT ?",
                                   (kind, limit)).fetchall()
        return [(rid, json.loads(body)) for rid, body in rows]

    def ack(self, ids):
        if not ids:
            return
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("DELETE FROM outbox WHERE id=?", [(i,) for i in ids])
            self.db.execute("COMMIT")

    def pending(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def trim(self):
        """If the disk buffer grows past max_rows (months offline: ~20 M rows is about six
        months for 15 meters), drop the oldest readings that carry no energy delta.
        Energy deltas and events are never dropped: billing needs them."""
        n = self.pending()
        if n <= self.max_rows:
            return 0
        with self.lock:
            cur = self.db.execute(
                "DELETE FROM outbox WHERE id IN (SELECT id FROM outbox WHERE kind='reading' "
                "AND body NOT LIKE '%\"d_kwh\"%' ORDER BY id LIMIT ?)", (n - self.max_rows,))
            return cur.rowcount

    # ---- energy bookkeeping --------------------------------------------
    def energy_delta(self, device_id, kwh, ts, max_kwh_per_h=1000.0):
        """Return the kWh used since the previous reading of this device (or None for the first).

        Guards: a negative delta (meter replaced / reset) or an impossible jump is
        not counted; the new value becomes the baseline.
        """
        with self.lock:
            row = self.db.execute("SELECT kwh, ts FROM last_energy WHERE device_id=?", (device_id,)).fetchone()
            self.db.execute("INSERT INTO last_energy(device_id, kwh, ts) VALUES (?,?,?) "
                            "ON CONFLICT(device_id) DO UPDATE SET kwh=excluded.kwh, ts=excluded.ts",
                            (device_id, kwh, ts))
        if row is None:
            return None
        prev_kwh, prev_ts = row
        delta = kwh - prev_kwh
        hours = max((ts - prev_ts) / 3600.0, 1 / 3600.0)
        if delta < 0 or delta > max_kwh_per_h * hours:
            return None
        return round(delta, 4)

    def get_state(self, k, default=None):
        with self.lock:
            row = self.db.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set_state(self, k, v):
        with self.lock:
            self.db.execute("INSERT INTO state(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def next_seq(self):
        with self.lock:
            row = self.db.execute("SELECT v FROM state WHERE k='seq'").fetchone()
            seq = int(row[0]) + 1 if row else 1
            self.db.execute("INSERT INTO state(k, v) VALUES ('seq', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(seq),))
        return seq
