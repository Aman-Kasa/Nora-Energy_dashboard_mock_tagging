import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server", "app"))

import yaml  # noqa: E402

from billing_core import block_cost, compute  # noqa: E402

TARIFFS = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "server", "app", "tariffs.yaml")))


def month(**over):
    data = {
        "shops": [
            {"id": "shop-A", "name": "A", "area_m2": 100, "grid_kwh": 900.0, "gen_kwh": 100.0},
            {"id": "shop-B", "name": "B", "area_m2": 300, "grid_kwh": 2700.0, "gen_kwh": 300.0},
        ],
        "main_grid_kwh": 4000.0,   # 400 kWh common on grid
        "gen_kwh": 500.0,          # 100 kWh common on generator
        "run_hours": 20.0,
        "fuel_litres": 150.0,
    }
    data.update(over)
    return data


class TestBilling(unittest.TestCase):
    def test_block_cost_rura_2025(self):
        self.assertEqual(block_cost(50, TARIFFS["grid_blocks"]), 50 * 355)
        self.assertEqual(block_cost(1000, TARIFFS["grid_blocks"]), 100 * 355 + 900 * 376)

    def test_everything_is_billed(self):
        r = compute(month(), TARIFFS)
        cost = r["totals"]["grid_cost_rwf"] + r["totals"]["gen_cost_rwf"]
        self.assertLessEqual(abs(r["totals"]["billed_rwf"] - cost), len(r["lines"]) * 3)  # rounding only

    def test_generator_rate(self):
        r = compute(month(), TARIFFS)
        expected = (150 * 2927 + 20 * TARIFFS["gen_maintenance_rwf_per_hour"]) / 500
        self.assertAlmostEqual(r["rates"]["gen_rwf_per_kwh"], expected, places=1)

    def test_eucl_bill_overrides_tariff(self):
        r = compute(month(eucl_bill_rwf=2_000_000), TARIFFS)
        self.assertAlmostEqual(r["rates"]["grid_rwf_per_kwh"], 500.0)

    def test_common_split_by_area(self):
        r = compute(month(), TARIFFS)
        a, b = r["lines"]
        self.assertAlmostEqual(b["common_rwf"] / a["common_rwf"], 3.0, places=2)

    def test_generator_energy_costs_more(self):
        r = compute(month(), TARIFFS)
        self.assertGreater(r["rates"]["gen_rwf_per_kwh"], r["rates"]["grid_rwf_per_kwh"])

    def test_meter_error_warning(self):
        r = compute(month(main_grid_kwh=3000.0), TARIFFS)
        self.assertTrue(any("exceed" in w for w in r["warnings"]))

    def test_fuel_estimate_warning(self):
        r = compute(month(fuel_litres=None), TARIFFS)
        self.assertTrue(any("estimated" in w for w in r["warnings"]))


if __name__ == "__main__":
    unittest.main()
