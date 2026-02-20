#!/usr/bin/env python3
import os
import sqlite3
from datetime import datetime, date, timedelta
from flask import Flask, request, redirect, url_for, render_template

app = Flask(__name__)
DB_PATH = os.environ.get("EMS_DB", "ems.db")

# Settings (edit later)
TARIFF_RWF_PER_KWH = float(os.environ.get("TARIFF_RWF_PER_KWH", "255"))
FORECAST_WINDOW_DAYS = int(os.environ.get("FORECAST_WINDOW_DAYS", "7"))

# Tagging thresholds (kWh) to detect which source is active in an interval
GEN_THRESHOLD_KWH = float(os.environ.get("GEN_THRESHOLD_KWH", "0.0001"))
UTIL_THRESHOLD_KWH = float(os.environ.get("UTIL_THRESHOLD_KWH", "0.0001"))

TENANTS = [
    {"id": 13, "name": "Supermarket"},
]

UTILITY_METER_ID = 10
GENERATOR_METER_ID = 21

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def first_after(conn, meter_id, start_ts, role=None):
    q = "SELECT ts, epp_kwh FROM readings WHERE meter_id=? AND ts >= ?"
    args = [meter_id, start_ts]
    if role:
        q += " AND role=?"
        args.append(role)
    q += " ORDER BY ts ASC LIMIT 1"
    return conn.execute(q, args).fetchone()

def last_before(conn, meter_id, end_ts, role=None):
    q = "SELECT ts, epp_kwh FROM readings WHERE meter_id=? AND ts <= ?"
    args = [meter_id, end_ts]
    if role:
        q += " AND role=?"
        args.append(role)
    q += " ORDER BY ts DESC LIMIT 1"
    return conn.execute(q, args).fetchone()

def kwh_in_range(conn, meter_id, start_ts, end_ts, role=None):
    s = first_after(conn, meter_id, start_ts, role=role)
    e = last_before(conn, meter_id, end_ts, role=role)
    if not s or not e:
        return None
    used = float(e["epp_kwh"]) - float(s["epp_kwh"])
    if used < 0:
        return None
    return {
        "start_ts": s["ts"], "start_kwh": float(s["epp_kwh"]),
        "end_ts": e["ts"], "end_kwh": float(e["epp_kwh"]),
        "used_kwh": used
    }

def get_first_topup(conn):
    return conn.execute("SELECT ts, amount_rwf, note FROM topups ORDER BY ts ASC LIMIT 1").fetchone()

def sum_topups_since(conn, start_ts):
    r = conn.execute("SELECT COALESCE(SUM(amount_rwf),0) AS s FROM topups WHERE ts >= ?", (start_ts,)).fetchone()
    return float(r["s"])

def daily_utility_cost(conn, day_str):
    start_ts = f"{day_str} 00:01:00"
    end_ts = f"{day_str} 23:59:00"
    info = kwh_in_range(conn, UTILITY_METER_ID, start_ts, end_ts, role="utility")
    if not info:
        return None
    cost = info["used_kwh"] * TARIFF_RWF_PER_KWH
    return info["used_kwh"], cost

def tenant_split_by_tag(conn, tenant_id, start_ts, end_ts):
    """Split tenant interval deltas into UTILITY vs GENERATOR using the agreed tagging rule:
       - if generator main meter increases in an interval -> tag tenant delta as GENERATOR
       - else if utility main meter increases -> tag tenant delta as UTILITY
    """
    util_rows = conn.execute(
        "SELECT ts, epp_kwh FROM readings WHERE meter_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (UTILITY_METER_ID, start_ts, end_ts),
    ).fetchall()
    gen_rows = conn.execute(
        "SELECT ts, epp_kwh FROM readings WHERE meter_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (GENERATOR_METER_ID, start_ts, end_ts),
    ).fetchall()
    ten_rows = conn.execute(
        "SELECT ts, epp_kwh FROM readings WHERE meter_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (tenant_id, start_ts, end_ts),
    ).fetchall()

    if len(util_rows) < 2 or len(gen_rows) < 2 or len(ten_rows) < 2:
        return None

    util = {r["ts"]: float(r["epp_kwh"]) for r in util_rows}
    gen  = {r["ts"]: float(r["epp_kwh"]) for r in gen_rows}
    ten  = {r["ts"]: float(r["epp_kwh"]) for r in ten_rows}

    # common timestamps (poller writes all three meters at nearly the same time)
    ts_list = sorted(set(ten.keys()) & set(util.keys()) & set(gen.keys()))
    if len(ts_list) < 2:
        return None

    utility_kwh = 0.0
    generator_kwh = 0.0
    unknown_kwh = 0.0
    last_source = None

    # store a small audit trail for the UI
    audit = []  # each: {t0,t1, d_util,d_gen,d_ten, source}

    for i in range(1, len(ts_list)):
        t0, t1 = ts_list[i-1], ts_list[i]
        d_util = util[t1] - util[t0]
        d_gen  = gen[t1]  - gen[t0]
        d_ten  = ten[t1]  - ten[t0]

        if d_ten < 0:
            continue

        source = None
        if d_gen > GEN_THRESHOLD_KWH:
            source = "GENERATOR"
            generator_kwh += d_ten
            last_source = source
        elif d_util > UTIL_THRESHOLD_KWH:
            source = "UTILITY"
            utility_kwh += d_ten
            last_source = source
        else:
            # no clear signal; keep last known source if available
            source = last_source if last_source else "UNKNOWN"
            if source == "GENERATOR":
                generator_kwh += d_ten
            elif source == "UTILITY":
                utility_kwh += d_ten
            else:
                unknown_kwh += d_ten

        audit.append({
            "t0": t0, "t1": t1,
            "d_util": d_util, "d_gen": d_gen, "d_ten": d_ten,
            "source": source
        })

    return {
        "utility_kwh": utility_kwh,
        "generator_kwh": generator_kwh,
        "unknown_kwh": unknown_kwh,
        "total_kwh": utility_kwh + generator_kwh + unknown_kwh,
        "audit": audit[-30:],  # show last 30 intervals
    }

@app.get("/")
def home():
    return redirect(url_for("dashboard"))

@app.get("/dashboard")
def dashboard():
    day = request.args.get("day", "2026-02-20")
    start = request.args.get("start", f"{day} 00:01:00")
    end = request.args.get("end", f"{day} 23:59:00")

    with db() as conn:
        tenants_out = []
        for t in TENANTS:
            total = kwh_in_range(conn, t["id"], start, end, role="tenant")
            split = tenant_split_by_tag(conn, t["id"], start, end)
            tenants_out.append({"name": t["name"], "id": t["id"], "total": total, "split": split})

        util_info = kwh_in_range(conn, UTILITY_METER_ID, start, end, role="utility")
        gen_info = kwh_in_range(conn, GENERATOR_METER_ID, start, end, role="generator")
        util_cost = (util_info["used_kwh"] * TARIFF_RWF_PER_KWH) if util_info else None

        # Credit tracking from first topup
        first_topup = get_first_topup(conn)
        credit = None
        recent_daily = []
        if first_topup:
            first_ts = first_topup["ts"]
            total_topups = sum_topups_since(conn, first_ts)

            days = conn.execute("""
                SELECT DISTINCT substr(ts,1,10) AS day
                FROM readings
                WHERE meter_id=? AND ts >= ?
                ORDER BY day ASC
            """, (UTILITY_METER_ID, first_ts)).fetchall()

            spend = 0.0
            for d in days:
                k = daily_utility_cost(conn, d["day"])
                if k:
                    kwh, cost = k
                    spend += cost
                    recent_daily.append({"day": d["day"], "kwh": kwh, "cost": cost})

            balance = total_topups - spend
            last_days = [x["day"] for x in recent_daily][-FORECAST_WINDOW_DAYS:]
            costs = [x["cost"] for x in recent_daily if x["day"] in last_days]
            avg_daily = (sum(costs) / len(costs)) if costs else None
            days_left = (balance / avg_daily) if (avg_daily and avg_daily > 0) else None

            credit = {
                "first_topup_ts": first_ts,
                "total_topups": total_topups,
                "total_spend": spend,
                "balance_left": balance,
                "avg_daily_cost": avg_daily,
                "days_left": days_left,
            }

        # chart (last 10 days)
        chart = {"labels": [], "utility_kwh": [], "generator_kwh": [], "tenant_kwh": []}
        days_for_chart = conn.execute("""
            SELECT DISTINCT substr(ts,1,10) AS day
            FROM readings
            WHERE ts <= ?
            ORDER BY day DESC
            LIMIT 10
        """, (end,)).fetchall()
        days_for_chart = list(reversed([d["day"] for d in days_for_chart]))
        for d in days_for_chart:
            start_ts_d = f"{d} 00:01:00"
            end_ts_d = f"{d} 23:59:00"
            u = kwh_in_range(conn, UTILITY_METER_ID, start_ts_d, end_ts_d, role="utility")
            g = kwh_in_range(conn, GENERATOR_METER_ID, start_ts_d, end_ts_d, role="generator")
            t = kwh_in_range(conn, TENANTS[0]["id"], start_ts_d, end_ts_d, role="tenant")
            chart["labels"].append(d)
            chart["utility_kwh"].append(round(u["used_kwh"], 3) if u else 0)
            chart["generator_kwh"].append(round(g["used_kwh"], 3) if g else 0)
            chart["tenant_kwh"].append(round(t["used_kwh"], 3) if t else 0)

        return render_template(
            "dashboard.html",
            tz_note="Africa/Kigali (timestamps stored as local time)",
            day=day, start=start, end=end,
            tariff=TARIFF_RWF_PER_KWH,
            thresholds={"gen": GEN_THRESHOLD_KWH, "util": UTIL_THRESHOLD_KWH},
            tenants=tenants_out,
            util_info=util_info, gen_info=gen_info,
            util_cost=util_cost,
            credit=credit,
            recent_daily=recent_daily[-14:],
            chart_json=chart
        )

@app.post("/topup")
def add_topup():
    amount = request.form.get("amount_rwf", "").strip()
    note = request.form.get("note", "").strip()
    ts = request.form.get("ts", "").strip()
    if not ts:
        ts = datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

    try:
        amt = float(amount)
        if amt <= 0:
            raise ValueError()
    except Exception:
        return redirect(url_for("dashboard"))

    with db() as conn:
        conn.execute("INSERT INTO topups (ts, amount_rwf, note) VALUES (?,?,?)", (ts, amt, note))
        conn.commit()

    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
