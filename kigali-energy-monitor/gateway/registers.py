"""Modbus register maps for the devices used in the building.

Each map entry:
    name     -> key used in the reading (p_kw, kwh, v, ...)
    addr     -> 0-based register address (as sent on the wire)
    fc       -> 3 = holding registers, 4 = input registers
    type     -> float32 | uint16 | int16 | uint32 | int32
    scale    -> multiply the raw value by this
    group    -> "fast" (every poll_fast_s) or "slow" (every poll_slow_s)

Eastron addresses come from the Eastron SDM120 / SDM630 Modbus protocol
documents (input registers, IEEE754 float, big-endian, high word first).

Deep Sea (DSE) GenComm addresses vary by controller family and firmware.
The map below follows GenComm page 4 / page 6 / page 7 for the DSE 7xxx/8xxx
series. VERIFY every address against the GenComm document for the exact
controller on site before trusting the values.
"""

SDM120 = [
    # name,     addr,   fc, type,      scale,  group
    ("v",       0x0000, 4, "float32", 1.0,    "fast"),
    ("i",       0x0006, 4, "float32", 1.0,    "fast"),
    ("p_kw",    0x000C, 4, "float32", 0.001,  "fast"),   # W -> kW
    ("pf",      0x001E, 4, "float32", 1.0,    "fast"),
    ("hz",      0x0046, 4, "float32", 1.0,    "fast"),
    ("kwh",     0x0048, 4, "float32", 1.0,    "fast"),   # import active energy (every poll: keeps outage splits sharp)
]

SDM630 = [
    ("v_l1",    0x0000, 4, "float32", 1.0,    "fast"),
    ("v_l2",    0x0002, 4, "float32", 1.0,    "fast"),
    ("v_l3",    0x0004, 4, "float32", 1.0,    "fast"),
    ("i_l1",    0x0006, 4, "float32", 1.0,    "fast"),
    ("i_l2",    0x0008, 4, "float32", 1.0,    "fast"),
    ("i_l3",    0x000A, 4, "float32", 1.0,    "fast"),
    ("p_kw",    0x0034, 4, "float32", 0.001,  "fast"),   # total system W -> kW
    ("pf",      0x003E, 4, "float32", 1.0,    "fast"),
    ("hz",      0x0046, 4, "float32", 1.0,    "fast"),
    ("kwh",     0x0048, 4, "float32", 1.0,    "fast"),
]

# Deep Sea GenComm (holding registers). VERIFY ON SITE.
DSE_GENCOMM = [
    ("oil_kpa",      1024, 3, "uint16", 1.0,   "fast"),
    ("coolant_c",    1025, 3, "int16",  1.0,   "fast"),
    ("fuel_pct",     1027, 3, "uint16", 1.0,   "fast"),
    ("battery_v",    1029, 3, "uint16", 0.1,   "fast"),
    ("rpm",          1030, 3, "uint16", 1.0,   "fast"),
    ("gen_hz",       1031, 3, "uint16", 0.1,   "fast"),
    ("gen_p_kw",     1536, 3, "int32",  0.001, "fast"),  # total watts -> kW
    ("run_seconds",  1798, 3, "uint32", 1.0,   "slow"),  # engine run time
    ("alarm_word",   2048, 3, "uint16", 1.0,   "fast"),  # first alarm word (page 8)
]

MAPS = {
    "sdm120": SDM120,
    "sdm630": SDM630,
    "sdm72d": SDM630,      # same addresses for the values we read
    "dse_gencomm": DSE_GENCOMM,
}

TYPE_WORDS = {"float32": 2, "uint32": 2, "int32": 2, "uint16": 1, "int16": 1}


def get_map(model: str):
    try:
        return MAPS[model.lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown device model '{model}'. Known: {sorted(MAPS)}") from exc


def plan_blocks(regmap, group, max_count=60):
    """Merge registers of one group and function code into as few block reads as possible.

    Returns a list of (fc, start, count, [entries]).
    Gaps between registers are read too (cheaper than a new request),
    as long as the block stays under max_count registers.
    """
    entries = sorted((e for e in regmap if e[5] == group), key=lambda e: (e[2], e[1]))
    blocks = []
    for e in entries:
        name, addr, fc, typ, scale, _ = e
        end = addr + TYPE_WORDS[typ]
        if blocks and blocks[-1][0] == fc and end - blocks[-1][1] <= max_count and addr >= blocks[-1][1]:
            fc0, start, count, items = blocks[-1]
            blocks[-1] = (fc0, start, max(count, end - start), items + [e])
        else:
            blocks.append((fc, addr, end - addr, [e]))
    return blocks
