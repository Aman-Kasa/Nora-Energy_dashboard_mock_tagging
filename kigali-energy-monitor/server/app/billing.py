#!/usr/bin/env python3
"""Monthly billing job.

  python billing.py --site kigali-mall --month 2026-11 --run-by aman
  python billing.py --site kigali-mall --month 2026-11 --eucl-bill 5400000 --fuel-litres 820 --run-by aman
  python billing.py ... --dry-run          # print, do not save

Month boundaries are Kigali local time (UTC+2). Writes the `bills` table,
a `billing_runs` audit row, and a CSV file per month.
"""
import argparse
import csv
import json
import os
from datetime import date, datetime, timedelta, timezone

import psycopg
import yaml

from billing_core import compute

KIGALI = timezone(timedelta(hours=2))
DSN = os.environ.get("DATABASE_URL", "postgresql://energy:energy@db:5432/energy")


def month_bounds(month):
    y, m = map(int, month.split("-"))
    start = datetime(y, m, 1, tzinfo=KIGALI)
    end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=KIGALI)
    return start, end


def load_month(cur, site, start, end, args):
    cur.execute("SELECT device_id, role, name, area_m2, tank_litres FROM devices WHERE site=%s AND active",
                (site,))
    devs = {r[0]: {"role": r[1], "name": r[2], "area_m2": r[3], "tank": r[4]} for r in cur.fetchall()}

    cur.execute("""SELECT device_id, source, coalesce(sum(kwh), 0) FROM energy_hourly
                   WHERE site=%s AND bucket >= %s AND bucket < %s GROUP BY 1, 2""", (site, start, end))
    energy = {}
    for dev, src, kwh in cur.fetchall():
        energy.setdefault(dev, {})[src] = float(kwh)

    shops = [{"id": d, "name": v["name"], "area_m2": float(v["area_m2"] or 0),
              "grid_kwh": energy.get(d, {}).get("GRID", 0.0),
              "gen_kwh": energy.get(d, {}).get("GENERATOR", 0.0)}
             for d, v in devs.items() if v["role"] == "shop"]
    main_kwh = sum(sum(energy.get(d, {}).values()) for d, v in devs.items() if v["role"] == "main")
    gen_meters = [d for d, v in devs.items() if v["role"] == "generator_meter"]
    gen_kwh = sum(sum(energy.get(d, {}).values()) for d in gen_meters)
    warnings = []
    if not gen_meters:
        gen_kwh = sum(s["gen_kwh"] for s in shops)
        warnings.append("No generator meter: generator kWh = sum of shop generator kWh (common areas not covered).")

    # generator run hours and fuel from the controller
    run_hours, litres = 0.0, None
    for d, v in devs.items():
        if v["role"] != "genset":
            continue
        cur.execute("""SELECT max((m->>'run_seconds')::float) - min((m->>'run_seconds')::float)
                       FROM readings WHERE device_id=%s AND ts >= %s AND ts < %s AND m ? 'run_seconds'""",
                    (d, start, end))
        run_hours += (cur.fetchone()[0] or 0) / 3600
        if v["tank"]:
            # fuel used = level just before each generator run minus level just after it
            # (averaged over 10 minutes to smooth the float sensor; refuels happen between runs)
            cur.execute("""WITH runs AS (
                             SELECT started, coalesce(ended, now()) AS ended FROM source_periods
                             WHERE site=%s AND source='GENERATOR' AND started >= %s AND started < %s)
                           SELECT coalesce(sum(greatest(b.pct - a.pct, 0)), 0) FROM runs,
                             LATERAL (SELECT avg((m->>'fuel_pct')::float) pct FROM readings
                                      WHERE device_id=%s AND m ? 'fuel_pct'
                                        AND ts BETWEEN runs.started - interval '10 minutes' AND runs.started) b,
                             LATERAL (SELECT avg((m->>'fuel_pct')::float) pct FROM readings
                                      WHERE device_id=%s AND m ? 'fuel_pct'
                                        AND ts BETWEEN runs.ended AND runs.ended + interval '10 minutes') a""",
                        (site, start, end, d, d))
            litres = (litres or 0) + float(cur.fetchone()[0]) * float(v["tank"]) / 100

    cur.execute("SELECT eucl_bill_rwf, fuel_litres, diesel_rwf_l FROM month_inputs WHERE site=%s AND month=%s",
                (site, start.date()))
    row = cur.fetchone() or (None, None, None)
    data = {"shops": shops, "main_grid_kwh": main_kwh, "gen_kwh": gen_kwh, "run_hours": run_hours,
            "eucl_bill_rwf": args.eucl_bill or row[0],
            "fuel_litres": args.fuel_litres if args.fuel_litres is not None else (row[1] if row[1] is not None else litres),
            "diesel_rwf_l": args.diesel or row[2]}
    return data, warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--month", default=(date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
                    help="YYYY-MM (default: last month)")
    ap.add_argument("--eucl-bill", type=float, help="the building's EUCL bill for the month, RWF incl. VAT")
    ap.add_argument("--fuel-litres", type=float, help="diesel used, from receipts / dipstick")
    ap.add_argument("--diesel", type=float, help="diesel price RWF/L if different from tariffs.yaml")
    ap.add_argument("--tariffs", default=os.path.join(os.path.dirname(__file__), "tariffs.yaml"))
    ap.add_argument("--out", default=os.environ.get("BILL_DIR", "./bills"))
    ap.add_argument("--run-by", required=True, help="who is running the bills (audit log)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.tariffs) as f:
        tariffs = yaml.safe_load(f)
    start, end = month_bounds(args.month)
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        data, warns = load_month(cur, args.site, start, end, args)
        result = compute(data, tariffs)
        result["warnings"] = warns + result["warnings"]

        print(f"\nBills for {args.site}, {args.month}")
        print(json.dumps(result["rates"], indent=2))
        print(f"{'Shop':<24}{'grid kWh':>10}{'gen kWh':>10}{'grid RWF':>12}{'gen RWF':>12}{'common':>10}{'TOTAL RWF':>12}")
        for l in result["lines"]:
            print(f"{(l['shop_name'] or l['device_id'])[:23]:<24}{l['grid_kwh']:>10.1f}{l['gen_kwh']:>10.1f}"
                  f"{l['grid_rwf']:>12,}{l['gen_rwf']:>12,}{l['common_rwf']:>10,}{l['total_rwf']:>12,}")
        print(json.dumps(result["totals"], indent=2))
        for w in result["warnings"]:
            print("WARNING:", w)
        if args.dry_run:
            return

        cur.execute("INSERT INTO billing_runs(site, month, run_by, inputs, summary) VALUES (%s,%s,%s,%s,%s) "
                    "RETURNING run_id", (args.site, start.date(), args.run_by,
                                         json.dumps({k: v for k, v in data.items() if k != "shops"}),
                                         json.dumps({**result["rates"], **result["totals"],
                                                     "warnings": result["warnings"]})))
        run_id = cur.fetchone()[0]
        for l in result["lines"]:
            cur.execute("""INSERT INTO bills(site, month, device_id, shop_name, grid_kwh, gen_kwh, common_kwh,
                                             grid_rwf, gen_rwf, common_rwf, total_rwf, run_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (site, month, device_id) DO UPDATE SET
                             shop_name=excluded.shop_name, grid_kwh=excluded.grid_kwh, gen_kwh=excluded.gen_kwh,
                             common_kwh=excluded.common_kwh, grid_rwf=excluded.grid_rwf, gen_rwf=excluded.gen_rwf,
                             common_rwf=excluded.common_rwf, total_rwf=excluded.total_rwf, run_id=excluded.run_id""",
                        (args.site, start.date(), l["device_id"], l["shop_name"], l["grid_kwh"], l["gen_kwh"],
                         l["common_kwh"], l["grid_rwf"], l["gen_rwf"], l["common_rwf"], l["total_rwf"], run_id))

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"bills_{args.site}_{args.month}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(result["lines"][0].keys()) if result["lines"] else ["device_id"])
        w.writeheader()
        w.writerows(result["lines"])
    print(f"\nSaved run {run_id} and {path}")


if __name__ == "__main__":
    main()
