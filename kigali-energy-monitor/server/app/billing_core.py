"""Pure billing maths (no database), so it can be unit-tested and audited.

Per shop, per month:
    grid_rwf   = shop grid kWh       x grid rate
    gen_rwf    = shop generator kWh  x generator rate
    common_rwf = share of (common-area + loss energy) cost
    total_rwf  = grid_rwf + gen_rwf + common_rwf

grid rate:
    the building's real EUCL bill / main-meter kWh, if the bill is entered;
    else the RURA non-residential block tariff applied to the building's kWh, plus VAT.
generator rate:
    (litres x diesel price + run hours x maintenance rate) / generator kWh
"""


def block_cost(kwh, blocks):
    """Cost of `kwh` under increasing block tariffs [{upto: 100, rate: 355}, {upto: None, rate: 376}]."""
    cost, prev = 0.0, 0.0
    for b in blocks:
        upto = b.get("upto")
        top = kwh if upto is None else min(kwh, upto)
        if top > prev:
            cost += (top - prev) * b["rate"]
            prev = top
        if upto is not None and kwh <= upto:
            break
    return cost


def _weights(shops, rule):
    if rule == "area":
        w = {s["id"]: float(s.get("area_m2") or 0) for s in shops}
    elif rule == "kwh":
        w = {s["id"]: float(s["grid_kwh"] + s["gen_kwh"]) for s in shops}
    else:
        w = {s["id"]: 1.0 for s in shops}
    total = sum(w.values())
    if total <= 0:                       # missing areas -> fall back to equal shares
        return {s["id"]: 1 / len(shops) for s in shops} if shops else {}
    return {k: v / total for k, v in w.items()}


def compute(data, tariffs):
    """data: dict with shops, main_grid_kwh, gen_kwh, run_hours, fuel_litres?, eucl_bill_rwf?, diesel_rwf_l?
    Returns a dict: rates, lines (one per shop), totals, warnings."""
    warnings = []
    vat = float(tariffs.get("vat", 0.18))
    shops = data["shops"]
    main_kwh = float(data.get("main_grid_kwh") or 0)
    gen_kwh = float(data.get("gen_kwh") or 0)

    # ---- grid rate -----------------------------------------------------
    if data.get("eucl_bill_rwf"):
        grid_cost = float(data["eucl_bill_rwf"])
        grid_rate = grid_cost / main_kwh if main_kwh else 0.0
        grid_basis = "EUCL bill / main meter kWh"
    else:
        grid_cost = block_cost(main_kwh, tariffs["grid_blocks"]) * (1 + vat)
        grid_rate = grid_cost / main_kwh if main_kwh else tariffs["grid_blocks"][-1]["rate"] * (1 + vat)
        grid_basis = "RURA non-residential blocks + VAT (no EUCL bill entered)"

    # ---- generator rate ------------------------------------------------
    diesel = float(data.get("diesel_rwf_l") or tariffs["diesel_rwf_per_litre"])
    litres = data.get("fuel_litres")
    if litres is None:
        litres = gen_kwh / float(tariffs.get("gen_kwh_per_litre", 3.3))
        warnings.append(f"Fuel used is estimated from kWh ({litres:.0f} L); enter real litres for an exact bill.")
    run_hours = float(data.get("run_hours") or 0)
    gen_cost = float(litres) * diesel + run_hours * float(tariffs.get("gen_maintenance_rwf_per_hour", 0))
    gen_rate = gen_cost / gen_kwh if gen_kwh else 0.0

    # ---- common areas and losses --------------------------------------
    shop_grid = sum(s["grid_kwh"] for s in shops)
    shop_gen = sum(s["gen_kwh"] for s in shops)
    common_grid = main_kwh - shop_grid
    common_gen = gen_kwh - shop_gen
    for label, val in (("grid", common_grid), ("generator", common_gen)):
        if val < 0:
            warnings.append(f"Shop meters exceed the {label} meter by {-val:.1f} kWh: check CTs and meter wiring.")
    common_grid, common_gen = max(common_grid, 0.0), max(common_gen, 0.0)
    common_cost = common_grid * grid_rate + common_gen * gen_rate
    total_in = main_kwh + gen_kwh
    if total_in and (common_grid + common_gen) / total_in > 0.25:
        warnings.append("Common areas + losses are over 25% of building energy: check for unmetered tenants.")

    shares = _weights(shops, tariffs.get("common_split", "area"))
    margin = 1 + float(tariffs.get("landlord_margin_pct", 0)) / 100

    lines = []
    for s in shops:
        g = s["grid_kwh"] * grid_rate * margin
        e = s["gen_kwh"] * gen_rate * margin
        c = common_cost * shares.get(s["id"], 0) * margin
        lines.append({
            "device_id": s["id"], "shop_name": s.get("name"),
            "grid_kwh": round(s["grid_kwh"], 2), "gen_kwh": round(s["gen_kwh"], 2),
            "common_kwh": round((common_grid + common_gen) * shares.get(s["id"], 0), 2),
            "grid_rwf": round(g), "gen_rwf": round(e), "common_rwf": round(c),
            "total_rwf": round(g) + round(e) + round(c),
        })
    lines.sort(key=lambda l: l["device_id"])
    return {
        "rates": {"grid_rwf_per_kwh": round(grid_rate, 2), "gen_rwf_per_kwh": round(gen_rate, 2),
                  "grid_basis": grid_basis, "diesel_rwf_per_litre": diesel, "fuel_litres": round(float(litres), 1),
                  "run_hours": round(run_hours, 2)},
        "totals": {"main_grid_kwh": round(main_kwh, 2), "gen_kwh": round(gen_kwh, 2),
                   "common_grid_kwh": round(common_grid, 2), "common_gen_kwh": round(common_gen, 2),
                   "grid_cost_rwf": round(grid_cost), "gen_cost_rwf": round(gen_cost),
                   "billed_rwf": sum(l["total_rwf"] for l in lines)},
        "lines": lines,
        "warnings": warnings,
    }
