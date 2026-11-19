#!/usr/bin/env python3
"""Energy gateway for a multi-tenant building in Kigali (Raspberry Pi 5).

Polls every meter over RS485/Modbus RTU, works out whether the building is on
GRID or GENERATOR, tags every reading with that source, stores it locally and
forwards it to the server over MQTT.

  python3 main.py --config /etc/energy-gateway/config.yaml          # real hardware
  python3 main.py --config config.example.yaml --simulate --dry-run  # bench test, no hardware
  python3 main.py --config config.example.yaml --simulate --speed 60 # 1 real s = 1 simulated min
"""
import argparse
import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

from buffer import Buffer
from publisher import MqttPublisher, PrintPublisher, forward_loop
from source import GENERATOR, GRID, NONE, AtsInput, SourceDetector

log = logging.getLogger("gateway")

ENERGY_KEYS = ("kwh",)


def sd_notify(msg):
    """Tell systemd we are alive (watchdog). No-op when not run by systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read()) / 1000, 1)
    except OSError:
        return None


class Gateway:
    def __init__(self, cfg, simulate=False, speed=1.0, dry_run=False, buffer_path=None, sim_start=None):
        self.cfg = cfg
        g = cfg["gateway"]
        self.gw_id, self.site = g["id"], g["site"]
        self.fast_s = float(g.get("poll_fast_s", 10))
        self.slow_s = float(g.get("poll_slow_s", 60))
        self.offline_after = int(g.get("offline_after_failures", 3))
        self.heartbeat_s = float(g.get("heartbeat_s", 300))
        self.devices = cfg["devices"]
        self.by_role = {}
        for d in self.devices:
            self.by_role.setdefault(d["role"], []).append(d)
        self.failures = {d["id"]: 0 for d in self.devices}
        self.offline = set()
        self.stop_event = threading.Event()
        self.fwd_stop = threading.Event()

        path = buffer_path or g.get("buffer_path", "buffer.db")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.buffer = Buffer(path)

        src = cfg.get("source", {})
        if simulate:
            from simulator import SimAts, SimBuilding, SimBus, SimClock
            self.clock = SimClock(speed=speed, start=sim_start)
            self.building = SimBuilding(self.devices, self.clock)
            self.buses = {b["name"]: SimBus(self.building) for b in cfg["buses"]}
            ats = SimAts(self.building) if src.get("ats_gpio") is not None else None
            if speed <= 0:      # step mode: every cycle advances simulated time by exactly poll_fast_s
                self.sleep = lambda s: (self.clock.advance(self.fast_s), self.stop_event.is_set())[1]
            else:
                self.sleep = lambda s: self.stop_event.wait(s / speed)
        else:
            from modbus_bus import ModbusBus
            self.clock = time.time
            self.building = None
            self.buses = {b["name"]: ModbusBus(b["name"], b["port"], baudrate=b.get("baudrate", 9600),
                                               parity=b.get("parity", "N"), stopbits=b.get("stopbits", 1))
                          for b in cfg["buses"]}
            ats = AtsInput(src["ats_gpio"], src.get("ats_closed_means", GENERATOR)) \
                if src.get("ats_gpio") is not None else None
            self.sleep = self.stop_event.wait
        self.detector = SourceDetector(ats, grid_v_min=src.get("grid_v_min", 180),
                                       gen_kw_min=src.get("gen_kw_min", 0.5),
                                       debounce_s=src.get("debounce_s", 3), clock=self.clock)
        self.detector.state = self.buffer.get_state("source")   # survive reboots
        self.last_powered = self.buffer.get_state("last_powered", "GRID")

        m = dict(cfg.get("mqtt", {}))
        if m.get("password_file") and os.path.exists(m["password_file"]):
            with open(m["password_file"]) as f:
                m["password"] = f.read().strip()
        self.publisher = PrintPublisher() if dry_run else MqttPublisher(m, self.gw_id, self.site)
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(self.buses)))

    # ---- polling -------------------------------------------------------
    def _poll_bus(self, bus_name, groups):
        bus, out = self.buses[bus_name], {}
        for d in (d for d in self.devices if d["bus"] == bus_name):
            vals, ok = {}, True
            for grp in groups:
                try:
                    vals.update(bus.read_group(d, grp))
                except IOError as exc:
                    ok = False
                    log.debug("read failed %s: %s", d["id"], exc)
                    break
            out[d["id"]] = vals if ok else None
        return out

    def poll(self, groups):
        results = {}
        for fut in [self.pool.submit(self._poll_bus, b, groups) for b in self.buses]:
            results.update(fut.result())
        return results

    def _first(self, role):
        return (self.by_role.get(role) or [None])[0]

    def signals(self, res):
        main, genm, genset = self._first("main"), self._first("generator_meter"), self._first("genset")
        grid_v = gen_kw = gen_running = None
        if main:
            r = res.get(main["id"])
            if r:
                grid_v = r.get("v_l1", r.get("v"))
            else:
                # The main meter is powered by the grid. If it is silent while other
                # devices on the same bus answer, the grid is most likely down.
                if any(res.get(d["id"]) for d in self.devices if d["bus"] == main["bus"] and d is not main):
                    grid_v = 0.0
        if genm and res.get(genm["id"]):
            gen_kw = res[genm["id"]].get("p_kw")
        if genset and res.get(genset["id"]):
            gs = res[genset["id"]]
            gen_running = (gs.get("rpm") or 0) > 300
            if gen_kw is None:
                gen_kw = gs.get("gen_p_kw")
        return dict(grid_v=grid_v, gen_kw=gen_kw, gen_running=gen_running)

    def record(self, res, ts, source):
        items = []
        role = {d["id"]: d["role"] for d in self.devices}
        state = self.detector.state
        for dev_id, vals in res.items():
            if vals is None:
                # Meters powered by a supply that is off are expected to be silent.
                expected = (role[dev_id] == "generator_meter" and state != GENERATOR) or \
                           (role[dev_id] == "main" and state != GRID) or \
                           (role[dev_id] == "shop" and state == NONE)
                if not expected:
                    self._fail(dev_id, ts)
                continue
            self._ok(dev_id, ts)
            item = {"ts": round(ts, 3), "dev": dev_id, "src": source, "m": vals}
            if source == NONE:
                # No energy can flow while the building is dark, so a kWh step seen
                # right after a blackout belongs to the source that was on before it.
                item["src"] = self.last_powered
            if "kwh" in vals:
                d = self.buffer.energy_delta(dev_id, vals["kwh"], ts)
                if d is not None:
                    item["d_kwh"] = d
            items.append(item)
        if items:
            self.buffer.put_many("reading", items)

    def _fail(self, dev_id, ts):
        self.failures[dev_id] += 1
        if self.failures[dev_id] == self.offline_after and dev_id not in self.offline:
            self.offline.add(dev_id)
            self.event(ts, "device_offline", dev=dev_id)

    def _ok(self, dev_id, ts):
        self.failures[dev_id] = 0
        if dev_id in self.offline:
            self.offline.discard(dev_id)
            self.event(ts, "device_online", dev=dev_id)

    def event(self, ts, kind, **data):
        log.info("event %s %s", kind, data)
        self.buffer.put("event", {"ts": round(ts, 3), "type": kind, **data})

    # ---- main loop ---------------------------------------------------
    def run(self, cycles=None):
        for b in self.buses.values():
            b.connect()
        self.publisher.start()
        fwd = threading.Thread(target=forward_loop, daemon=True,
                               args=(self.buffer, self.publisher, self.gw_id, self.site, self.fwd_stop))
        fwd.start()
        meta = [{k: v for k, v in d.items() if k not in ("bus",)} for d in self.devices]
        self.event(self.clock(), "devices", devices=meta)

        last_slow = last_hb = 0.0
        n = 0
        sd_notify("READY=1")
        while not self.stop_event.is_set():
            t_start = time.monotonic()
            if self.building:
                self.building.step()
            now = self.clock()
            do_slow = now - last_slow >= self.slow_s
            groups = ("fast", "slow") if do_slow else ("fast",)
            res = self.poll(groups)
            if do_slow:
                last_slow = now

            prev = self.detector.state
            change = self.detector.update(**self.signals(res))
            # Readings of this cycle cover the interval that just ended -> tag with the source
            # that was in force during it (the previous accepted state).
            tag = prev or self.detector.state
            self.record(res, now, tag)
            if change:
                old, new, reason = change
                self.buffer.set_state("source", new)
                if new in (GRID, GENERATOR):
                    self.last_powered = new
                    self.buffer.set_state("last_powered", new)
                self.event(now, "source_change", old=old, new=new, reason=reason)

            if now - last_hb >= self.heartbeat_s:
                last_hb = now
                self.event(now, "heartbeat", pending=self.buffer.pending(), cpu_c=cpu_temp(),
                           offline=sorted(self.offline), source=self.detector.state)
                self.buffer.trim()

            sd_notify("WATCHDOG=1")
            n += 1
            if cycles and n >= cycles:
                break
            elapsed_real = time.monotonic() - t_start
            speed = getattr(self.clock, "speed", 1.0)
            self.sleep(max(0.0, self.fast_s - elapsed_real * speed))

        # let the forwarder drain what it can
        deadline = time.time() + 5
        while self.buffer.pending() and time.time() < deadline:
            time.sleep(0.2)
        self.stop_event.set()
        self.fwd_stop.set()
        fwd.join(timeout=5)
        self.publisher.stop()
        for b in self.buses.values():
            b.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="/etc/energy-gateway/config.yaml")
    ap.add_argument("--simulate", action="store_true", help="use the simulated building instead of RS485")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="simulation speed-up factor; 0 = step mode (one poll per simulated interval, as fast as possible)")
    ap.add_argument("--sim-start", default=None, help="simulated start time, ISO format, e.g. 2026-10-01T00:00+02:00")
    ap.add_argument("--dry-run", action="store_true", help="print batches instead of sending to MQTT")
    ap.add_argument("--cycles", type=int, default=None, help="stop after N fast cycles (testing)")
    ap.add_argument("--buffer", default=None, help="override buffer_path")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    sim_start = None
    if args.sim_start:
        from datetime import datetime
        sim_start = datetime.fromisoformat(args.sim_start).timestamp()
    gw = Gateway(cfg, simulate=args.simulate, speed=args.speed, dry_run=args.dry_run,
                 buffer_path=args.buffer, sim_start=sim_start)
    signal.signal(signal.SIGTERM, lambda *_: gw.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: gw.stop_event.set())
    gw.run(cycles=args.cycles)


if __name__ == "__main__":
    main()
