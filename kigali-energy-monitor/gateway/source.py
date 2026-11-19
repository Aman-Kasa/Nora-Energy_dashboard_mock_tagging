"""Decide which source is feeding the building: GRID, GENERATOR or NONE.

Priority:
  1. The ATS auxiliary contact (a GPIO input), if one is wired.
  2. Fallback from meter readings: generator power above a threshold means
     GENERATOR; grid voltage present means GRID; otherwise NONE (blackout,
     typically the few seconds before the generator takes the load).

A new state must hold for `debounce_s` before it is accepted, so contact
bounce or a voltage dip does not create fake outages.
"""
import logging
import time

log = logging.getLogger("source")

GRID, GENERATOR, NONE = "GRID", "GENERATOR", "NONE"


class AtsInput:
    """Reads the ATS position from a Pi 5 GPIO through an optocoupler."""

    def __init__(self, gpio_pin, closed_means=GENERATOR, pull_up=True):
        from gpiozero import Button   # uses lgpio on the Pi 5
        self.button = Button(gpio_pin, pull_up=pull_up, bounce_time=0.05)
        self.closed_means = closed_means
        self.open_means = GRID if closed_means == GENERATOR else GENERATOR

    def read(self):
        return self.closed_means if self.button.is_pressed else self.open_means


class SourceDetector:
    def __init__(self, ats=None, grid_v_min=180.0, gen_kw_min=0.5, debounce_s=3.0, clock=time.time):
        self.ats = ats
        self.grid_v_min = grid_v_min
        self.gen_kw_min = gen_kw_min
        self.debounce_s = debounce_s
        self.clock = clock
        self.state = None          # accepted state
        self._candidate = None
        self._candidate_since = None
        self.last_reason = ""

    def raw_state(self, grid_v=None, gen_kw=None, gen_running=None):
        """Instant opinion from the available signals, with the reason."""
        if self.ats is not None:
            try:
                s = self.ats.read()
                # Sanity check: ATS says generator but the generator produces nothing
                # and grid voltage is fine -> trust the meters, log a mismatch.
                if s == GENERATOR and gen_kw is not None and gen_kw < self.gen_kw_min \
                        and grid_v is not None and grid_v >= self.grid_v_min and gen_running is False:
                    return GRID, "ats-mismatch:meters-say-grid"
                # ATS rests on the grid side during a blackout, before the generator
                # is ready. No grid voltage + no generator power = the building is dark.
                if s == GRID and grid_v is not None and grid_v < self.grid_v_min \
                        and (gen_kw is None or gen_kw < self.gen_kw_min):
                    return NONE, "ats-grid-but-no-voltage"
                return s, "ats"
            except Exception as exc:   # GPIO failure -> fall back to meters
                log.warning("ATS read failed, using meters: %s", exc)
        if gen_kw is not None and gen_kw >= self.gen_kw_min:
            return GENERATOR, "gen-meter-power"
        if gen_running and (grid_v is None or grid_v < self.grid_v_min):
            return GENERATOR, "genset-running"
        if grid_v is not None and grid_v >= self.grid_v_min:
            return GRID, "grid-voltage"
        if grid_v is None and gen_kw is None:
            return (self.state or GRID), "no-signal-hold"
        return NONE, "no-voltage"

    def update(self, **signals):
        """Feed new signals. Returns (old, new, reason) when the accepted state changes, else None."""
        now = self.clock()
        s, reason = self.raw_state(**signals)
        if self.state is None:
            self.state, self.last_reason = s, reason
            return (None, s, reason)
        if s == self.state:
            self._candidate = None
            return None
        if s != self._candidate:
            self._candidate, self._candidate_since = s, now
            return None
        if now - self._candidate_since >= self.debounce_s:
            old = self.state
            self.state, self.last_reason = s, reason
            self._candidate = None
            return (old, s, reason)
        return None
