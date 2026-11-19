"""A simulated building, so the whole stack can be tested with no hardware.

It behaves like the real devices from the gateway's point of view:
  * shop meters follow Kigali retail hours (busy ~08:00-21:00, low at night)
  * the grid fails at random; the building goes dark, the generator starts
    after `gen_start_delay_s`, the ATS switches, and back again when the grid returns
  * meters powered from a dead supply stop answering (IOError), just like real ones
  * the DSE controller (battery powered) keeps answering: fuel %, run time, power

Use a `speed` above 1 to compress time (speed=60 -> one simulated minute per real second).
"""
import math
import random
import threading
import time

from source import GENERATOR, GRID

KIGALI_UTC_OFFSET_H = 2


class SimClock:
    """speed > 0: simulated time runs `speed` times faster than real time.
    speed == 0: step mode, time only moves when advance() is called (exact, for tests)."""

    def __init__(self, speed=1.0, start=None):
        self.speed = float(speed)
        self.t0_real = time.time()
        self.t0_sim = start if start is not None else self.t0_real
        self.t_step = self.t0_sim

    def __call__(self):
        if self.speed <= 0:
            return self.t_step
        return self.t0_sim + (time.time() - self.t0_real) * self.speed

    def advance(self, seconds):
        self.t_step += seconds


def shop_profile(local_hour):
    """0..1 load factor for a Kigali retail shop by local hour."""
    if 8 <= local_hour < 21:
        # morning ramp, lunch peak, evening peak
        return 0.55 + 0.25 * math.sin((local_hour - 8) / 13 * math.pi) + (0.15 if 17 <= local_hour < 20 else 0)
    if 7 <= local_hour < 8 or 21 <= local_hour < 22:
        return 0.3
    return 0.08   # fridges, security lights, routers


class SimBuilding:
    def __init__(self, devices, clock, seed=7, outage_rate_per_h=0.12, gen_start_delay_s=20,
                 tank_litres=500, kwh_per_litre=3.3, common_kw=6.0):
        self.rng = random.Random(seed)
        self.clock = clock
        self.lock = threading.Lock()
        self.outage_rate_per_h = outage_rate_per_h
        self.gen_start_delay_s = gen_start_delay_s
        self.tank_litres = tank_litres
        self.kwh_per_litre = kwh_per_litre
        self.common_kw = common_kw
        self.shops = {}
        for d in devices:
            if d["role"] == "shop":
                big = d["model"] in ("sdm630", "sdm72d")
                peak = d.get("sim_peak_kw", self.rng.uniform(8, 25) if big else self.rng.uniform(1.5, 6))
                self.shops[d["id"]] = {"peak": peak, "kwh": self.rng.uniform(100, 5000), "p": 0.0,
                                       "night_hog": self.rng.random() < 0.1,
                                       "truth": {GRID: 0.0, GENERATOR: 0.0}}   # ground truth for tests
        self.main_kwh = self.rng.uniform(50000, 90000)
        self.gen_kwh = self.rng.uniform(3000, 9000)
        self.run_s = self.rng.uniform(1e6, 3e6)
        self.fuel_pct = 85.0
        self.grid_up = True
        self.outage_ends = None
        self.outage_started = None
        self.gen_on = False
        self.last_t = clock()

    # ---- physics step ----------------------------------------------
    def step(self):
        with self.lock:
            now = self.clock()
            dt = max(0.0, now - self.last_t)
            self.last_t = now
            local_hour = ((now / 3600) + KIGALI_UTC_OFFSET_H) % 24

            # grid outages
            if self.grid_up and self.rng.random() < 1 - math.exp(-self.outage_rate_per_h * dt / 3600):
                self.grid_up = False
                self.outage_started = now
                self.outage_ends = now + self.rng.uniform(5, 90) * 60
            if not self.grid_up and now >= self.outage_ends:
                self.grid_up, self.gen_on = True, False
            if not self.grid_up and not self.gen_on and now - self.outage_started >= self.gen_start_delay_s \
                    and self.fuel_pct > 2:
                self.gen_on = True

            powered = self.grid_up or self.gen_on
            total = 0.0
            for s in self.shops.values():
                f = shop_profile(local_hour)
                if s["night_hog"] and not (8 <= local_hour < 21):
                    f = 0.6
                s["p"] = s["peak"] * f * self.rng.uniform(0.9, 1.1) if powered else 0.0
                s["kwh"] += s["p"] * dt / 3600
                if powered:
                    s["truth"][GRID if self.grid_up else GENERATOR] += s["p"] * dt / 3600
                total += s["p"]
            total += self.common_kw if powered else 0.0
            if self.grid_up:
                self.main_kwh += total * dt / 3600
            elif self.gen_on:
                e = total * dt / 3600
                self.gen_kwh += e
                self.run_s += dt
                self.fuel_pct = max(0.0, self.fuel_pct - (e / self.kwh_per_litre) / self.tank_litres * 100)
            if self.fuel_pct < 15 and self.grid_up:      # the landlord refuels
                self.fuel_pct = 95.0
            self.total_kw = total
            return now

    def source(self):
        return GENERATOR if (not self.grid_up and self.gen_on) else GRID

    # ---- device views ------------------------------------------------
    def _meter(self, p_kw, kwh, three_phase, group):
        v = self.rng.uniform(225, 236)
        if group == "slow":
            return {}
        if three_phase:
            ia = p_kw * 1000 / (3 * v * 0.92) if p_kw else 0.0
            return {"v_l1": round(v, 1), "v_l2": round(v + self.rng.uniform(-3, 3), 1),
                    "v_l3": round(v + self.rng.uniform(-3, 3), 1), "i_l1": round(ia, 2), "i_l2": round(ia, 2),
                    "i_l3": round(ia, 2), "p_kw": round(p_kw, 3), "pf": 0.92, "hz": 50.0, "kwh": round(kwh, 3)}
        return {"v": round(v, 1), "i": round(p_kw * 1000 / (v * 0.93), 2), "p_kw": round(p_kw, 3),
                "pf": 0.93, "hz": 50.0, "kwh": round(kwh, 3)}

    def read(self, device, group):
        with self.lock:
            role, three = device["role"], device["model"] in ("sdm630", "sdm72d")
            if role == "main":
                if not self.grid_up:
                    raise IOError("timeout (meter unpowered)")
                return self._meter(self.total_kw, self.main_kwh, True, group)
            if role == "generator_meter":
                if not self.gen_on:
                    raise IOError("timeout (meter unpowered)")
                return self._meter(self.total_kw, self.gen_kwh, True, group)
            if role == "genset":
                if group == "slow":
                    return {"run_seconds": int(self.run_s)}
                return {"oil_kpa": 380 if self.gen_on else 0, "coolant_c": 82 if self.gen_on else 28,
                        "fuel_pct": round(self.fuel_pct, 1), "battery_v": 27.6 if self.gen_on else 25.4,
                        "rpm": 1500 if self.gen_on else 0, "gen_hz": 50.0 if self.gen_on else 0.0,
                        "gen_p_kw": round(self.total_kw, 3) if self.gen_on else 0.0,
                        "alarm_word": 1 if self.fuel_pct < 20 else 0}
            if role == "shop":
                if not (self.grid_up or self.gen_on):
                    raise IOError("timeout (meter unpowered)")
                s = self.shops[device["id"]]
                return self._meter(s["p"], s["kwh"], three, group)
        raise IOError(f"no simulated device for {device['id']}")


class SimBus:
    """Drop-in for ModbusBus."""

    def __init__(self, building, latency_s=0.0):
        self.building = building
        self.latency_s = latency_s

    def connect(self):
        return True

    def read_group(self, device, group):
        if self.latency_s:
            time.sleep(self.latency_s)
        return self.building.read(device, group)

    def close(self):
        pass


class SimAts:
    def __init__(self, building):
        self.building = building

    def read(self):
        return self.building.source()
