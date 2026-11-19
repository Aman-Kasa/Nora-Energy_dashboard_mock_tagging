"""Gateway tests. Run from the repo root:  python3 -m unittest discover -s tests -v"""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway"))

import yaml  # noqa: E402

from buffer import Buffer  # noqa: E402
from modbus_bus import decode, extract  # noqa: E402
from registers import SDM120, SDM630, plan_blocks  # noqa: E402
from source import GENERATOR, GRID, NONE, SourceDetector  # noqa: E402

CONFIG = os.path.join(os.path.dirname(__file__), "..", "gateway", "config.example.yaml")


def f32_words(x):
    hi, lo = struct.unpack(">HH", struct.pack(">f", x))
    return [hi, lo]


class TestDecode(unittest.TestCase):
    def test_float_big_endian(self):
        self.assertAlmostEqual(decode(f32_words(230.5), "float32"), 230.5, places=3)

    def test_float_little_word_order(self):
        w = f32_words(12.25)
        self.assertAlmostEqual(decode([w[1], w[0]], "float32", "little"), 12.25, places=3)

    def test_ints(self):
        self.assertEqual(decode([0xFFFF], "int16"), -1)
        self.assertEqual(decode([0x0001, 0x0000], "uint32"), 65536)

    def test_extract_block(self):
        words = [0] * 0x4A
        words[0x00:0x02] = f32_words(231.0)
        words[0x0C:0x0E] = f32_words(1500.0)       # W
        words[0x48:0x4A] = f32_words(1234.5)
        blocks = plan_blocks(SDM120, "fast", max_count=80)
        self.assertEqual(len(blocks), 1)
        fc, start, count, entries = blocks[0]
        vals = extract(start, words[start:start + count], entries)
        self.assertAlmostEqual(vals["v"], 231.0)
        self.assertAlmostEqual(vals["p_kw"], 1.5)
        self.assertAlmostEqual(vals["kwh"], 1234.5, places=2)

    def test_plan_splits_long_ranges(self):
        blocks = plan_blocks(SDM630, "fast", max_count=60)
        self.assertGreater(len(blocks), 1)
        for _fc, start, count, entries in blocks:
            self.assertLessEqual(count, 60)
            for e in entries:
                self.assertGreaterEqual(e[1], start)
                self.assertLessEqual(e[1] + 2, start + count)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeAts:
    def __init__(self, s):
        self.s = s

    def read(self):
        return self.s


class TestSource(unittest.TestCase):
    def test_meter_fallback(self):
        d = SourceDetector(None, clock=FakeClock())
        self.assertEqual(d.raw_state(grid_v=230, gen_kw=0)[0], GRID)
        self.assertEqual(d.raw_state(grid_v=0, gen_kw=12)[0], GENERATOR)
        self.assertEqual(d.raw_state(grid_v=0, gen_kw=0, gen_running=False)[0], NONE)

    def test_ats_blackout_detected(self):
        d = SourceDetector(FakeAts(GRID), clock=FakeClock())
        self.assertEqual(d.raw_state(grid_v=0.0, gen_kw=0.0)[0], NONE)

    def test_ats_mismatch_trusts_meters(self):
        d = SourceDetector(FakeAts(GENERATOR), clock=FakeClock())
        self.assertEqual(d.raw_state(grid_v=231, gen_kw=0.0, gen_running=False)[0], GRID)

    def test_debounce(self):
        clk = FakeClock()
        d = SourceDetector(None, debounce_s=3, clock=clk)
        self.assertEqual(d.update(grid_v=230, gen_kw=0), (None, GRID, "grid-voltage"))
        self.assertIsNone(d.update(grid_v=0, gen_kw=10))          # candidate only
        clk.t += 1
        self.assertIsNone(d.update(grid_v=0, gen_kw=10))          # not long enough
        clk.t += 3
        self.assertEqual(d.update(grid_v=0, gen_kw=10)[:2], (GRID, GENERATOR))
        clk.t += 1
        self.assertIsNone(d.update(grid_v=230, gen_kw=0))         # a blip back is ignored...
        self.assertIsNone(d.update(grid_v=0, gen_kw=10))          # ...and cancelled
        self.assertEqual(d.state, GENERATOR)


class TestBuffer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Buffer(os.path.join(self.tmp.name, "b.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_outbox_roundtrip(self):
        self.b.put_many("reading", [{"n": i} for i in range(5)])
        rows = self.b.peek("reading", 3)
        self.assertEqual([r[1]["n"] for r in rows], [0, 1, 2])
        self.b.ack([r[0] for r in rows])
        self.assertEqual(self.b.pending(), 2)

    def test_energy_delta(self):
        self.assertIsNone(self.b.energy_delta("m1", 100.0, 0))
        self.assertAlmostEqual(self.b.energy_delta("m1", 100.5, 60), 0.5)
        self.assertIsNone(self.b.energy_delta("m1", 10.0, 120))     # meter replaced: not counted
        self.assertAlmostEqual(self.b.energy_delta("m1", 10.2, 180), 0.2)
        self.assertIsNone(self.b.energy_delta("m1", 99999, 190))    # impossible jump

    def test_seq_increments_and_persists(self):
        a, b = self.b.next_seq(), self.b.next_seq()
        self.assertEqual(b, a + 1)
        b2 = Buffer(self.b.path)
        self.assertEqual(b2.next_seq(), b + 1)


class TestEndToEndSimulation(unittest.TestCase):
    """Run the real gateway loop on the simulated building and check that
    energy is attributed to grid vs generator correctly."""

    def test_energy_split_matches_truth(self):
        from main import Gateway
        with open(CONFIG) as f:
            cfg = yaml.safe_load(f)
        cfg["gateway"]["heartbeat_s"] = 3600
        with tempfile.TemporaryDirectory() as tmp:
            gw = Gateway(cfg, simulate=True, speed=0, dry_run=True, buffer_path=os.path.join(tmp, "b.db"))
            gw.building.outage_rate_per_h = 0.6        # plenty of outages
            gw.building.rng.seed(3)
            # start counting truth after the first reading (the gateway has no baseline before)
            gw.run(cycles=1)
            for s in gw.building.shops.values():
                s["truth"] = {GRID: 0.0, GENERATOR: 0.0}
            gw.stop_event.clear()
            gw.fwd_stop.clear()
            gw.run(cycles=2500)                         # ~7 simulated hours
            sent = [it for kind, p in gw.publisher.sent if kind == "reading" for it in p["items"]]
            events = [it for kind, p in gw.publisher.sent if kind == "event" for it in p["items"]]

        changes = [e for e in events if e["type"] == "source_change"]
        self.assertTrue(any(e["new"] == GENERATOR for e in changes), "simulation produced no outage")

        got = {}
        for it in sent:
            if it["dev"].startswith("shop-") and "d_kwh" in it:
                got.setdefault(it["src"], 0.0)
                got[it["src"]] += it["d_kwh"]
        truth_grid = sum(s["truth"][GRID] for s in gw.building.shops.values())
        truth_gen = sum(s["truth"][GENERATOR] for s in gw.building.shops.values())
        self.assertNotIn(NONE, got, "energy must never be tagged NONE")
        self.assertGreater(truth_gen, 0)
        # boundary intervals (one 10 s cycle per switch-over) may land on the other side
        self.assertAlmostEqual(got.get(GRID, 0) + got.get(GENERATOR, 0), truth_grid + truth_gen,
                               delta=0.01 * (truth_grid + truth_gen))
        self.assertAlmostEqual(got.get(GENERATOR, 0), truth_gen, delta=0.05 * truth_gen + 1.0)


if __name__ == "__main__":
    unittest.main()
