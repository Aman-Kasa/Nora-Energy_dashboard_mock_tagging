#!/usr/bin/env python3
"""Commissioning tool: find which Modbus addresses answer on one RS485 bus.

  python3 scan_bus.py --port /dev/ttyUSB0                 # scan 1..247 with an Eastron voltage read
  python3 scan_bus.py --port /dev/ttyUSB0 --unit 12 --model sdm120   # dump one meter

Read-only: it only sends function code 04/03 reads.
"""
import argparse

from modbus_bus import ModbusBus


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--parity", default="N")
    ap.add_argument("--unit", type=int)
    ap.add_argument("--model", default="sdm120")
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--last", type=int, default=247)
    a = ap.parse_args()

    bus = ModbusBus("scan", a.port, baudrate=a.baud, parity=a.parity, timeout=0.3, retries=0)
    bus.connect()
    if a.unit:
        dev = {"id": f"unit{a.unit}", "unit": a.unit, "model": a.model}
        for grp in ("fast", "slow"):
            print(grp, bus.read_group(dev, grp))
        return
    found = []
    for unit in range(a.first, a.last + 1):
        dev = {"id": f"unit{unit}", "unit": unit, "model": "sdm120"}
        try:
            v = bus.read_group(dev, "fast").get("v")
            print(f"unit {unit:3d}: answers, V={v}")
            found.append(unit)
        except IOError:
            pass
    print(f"\n{len(found)} device(s) found: {found}")


if __name__ == "__main__":
    main()
