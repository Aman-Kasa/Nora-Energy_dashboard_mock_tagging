#!/usr/bin/env python3
"""Generate the Grafana dashboard and alert rules from readable SQL.

  python3 scripts/build_dashboard.py
writes grafana/dashboards/building.json and grafana/provisioning/alerting/rules.yaml
"""
import json
import os

import yaml

DS = {"type": "grafana-postgresql-datasource", "uid": "energy-db"}
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
MONTH_START = "date_trunc('month', now() AT TIME ZONE 'Africa/Kigali') AT TIME ZONE 'Africa/Kigali'"
RATE = ("(SELECT (summary->>'{k}')::numeric FROM billing_runs ORDER BY run_at DESC LIMIT 1)")
GRID_RATE = f"coalesce({RATE.format(k='grid_rwf_per_kwh')}, 444)"     # 376 x 1.18 until a real run exists
GEN_RATE = f"coalesce({RATE.format(k='gen_rwf_per_kwh')}, 890)"       # ~2927 RWF/L / 3.3 kWh/L

_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def target(sql, fmt="table", ref="A"):
    return {"datasource": DS, "editorMode": "code", "format": fmt, "rawQuery": True, "rawSql": sql.strip(), "refId": ref}


def panel(kind, title, x, y, w, h, targets, **extra):
    p = {"id": nid(), "type": kind, "title": title, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets}
    p.update(extra)
    return p


def stat(title, sql, x, y, w=6, h=4, unit="none", mappings=None, thresholds=None, decimals=None):
    defaults = {"unit": unit, "mappings": mappings or [],
                "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return panel("stat", title, x, y, w, h, [target(sql)],
                 fieldConfig={"defaults": defaults, "overrides": []},
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "colorMode": "background", "graphMode": "none", "textMode": "value"})


SOURCE_MAP = [{"type": "value", "options": {
    "GRID": {"text": "GRID (REG)", "color": "green"},
    "GENERATOR": {"text": "GENERATOR", "color": "orange"},
    "NONE": {"text": "NO POWER", "color": "red"}}}]

panels = [
    stat("Building is on", """
SELECT data->>'new' AS source FROM events WHERE type = 'source_change' ORDER BY ts DESC LIMIT 1""",
         0, 0, mappings=SOURCE_MAP),
    stat("Shops load now", """
SELECT coalesce(sum(p_kw), 0) AS kw FROM latest_readings WHERE role = 'shop' AND ts > now() - interval '2 minutes'""",
         6, 0, unit="kwatt", decimals=1),
    stat("Generator fuel", """
SELECT (m->>'fuel_pct')::float AS fuel FROM readings r JOIN devices d USING (device_id)
WHERE d.role = 'genset' AND m ? 'fuel_pct' ORDER BY ts DESC LIMIT 1""",
         12, 0, unit="percent", decimals=0,
         thresholds={"mode": "absolute", "steps": [{"color": "red", "value": None},
                                                    {"color": "orange", "value": 20}, {"color": "green", "value": 40}]}),
    stat("Generator hours this month", f"""
SELECT coalesce(sum(extract(epoch FROM (least(coalesce(ended, now()), now()) - greatest(started, {MONTH_START})))), 0) / 3600 AS h
FROM source_periods WHERE source = 'GENERATOR' AND coalesce(ended, now()) > {MONTH_START}""",
         18, 0, unit="h", decimals=1),

    panel("timeseries", "Building supply: grid vs generator (kW)", 0, 4, 24, 8, [target("""
SELECT $__timeGroupAlias(ts, '1m'),
       CASE d.role WHEN 'main' THEN 'Grid (REG)' ELSE 'Generator' END AS metric,
       avg(p_kw) AS kw
FROM readings r JOIN devices d USING (device_id)
WHERE $__timeFilter(ts) AND d.role IN ('main', 'generator_meter')
GROUP BY 1, 2 ORDER BY 1""", fmt="time_series")],
          fieldConfig={"defaults": {"unit": "kwatt", "custom": {"fillOpacity": 25, "lineWidth": 1, "stacking": {"mode": "normal"}}},
                       "overrides": [
                           {"matcher": {"id": "byName", "options": "Grid (REG)"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}}]},
                           {"matcher": {"id": "byName", "options": "Generator"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]}]}),

    panel("table", "Shops right now", 0, 12, 12, 10, [target("""
SELECT name AS "Shop", round(p_kw::numeric, 2) AS "kW",
       round((100 * p_kw / nullif(contracted_kw, 0))::numeric, 0) AS "% of contract",
       round(v::numeric, 0) AS "V", round(i::numeric, 1) AS "A", round(pf::numeric, 2) AS "PF",
       source AS "Source", ts AS "Last reading"
FROM latest_readings WHERE role = 'shop' ORDER BY name""")],
          fieldConfig={"defaults": {}, "overrides": [
              {"matcher": {"id": "byName", "options": "% of contract"},
               "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                              {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                  {"color": "green", "value": None}, {"color": "orange", "value": 80}, {"color": "red", "value": 100}]}}]},
              {"matcher": {"id": "byName", "options": "Source"},
               "properties": [{"id": "mappings", "value": SOURCE_MAP}, {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}]}),

    panel("table", "Month to date per shop (estimate, RWF)", 12, 12, 12, 10, [target(f"""
SELECT d.name AS "Shop",
       round(coalesce(sum(e.kwh) FILTER (WHERE e.source = 'GRID'), 0)::numeric, 1) AS "Grid kWh",
       round(coalesce(sum(e.kwh) FILTER (WHERE e.source = 'GENERATOR'), 0)::numeric, 1) AS "Generator kWh",
       round(coalesce(sum(e.kwh) FILTER (WHERE e.source = 'GRID'), 0) * {GRID_RATE}
           + coalesce(sum(e.kwh) FILTER (WHERE e.source = 'GENERATOR'), 0) * {GEN_RATE}) AS "Est. RWF (excl. common)"
FROM devices d LEFT JOIN energy_hourly e ON e.device_id = d.device_id AND e.bucket >= {MONTH_START}
WHERE d.role = 'shop' GROUP BY d.name ORDER BY 4 DESC""")],
          fieldConfig={"defaults": {}, "overrides": [
              {"matcher": {"id": "byName", "options": "Est. RWF (excl. common)"},
               "properties": [{"id": "unit", "value": "locale"}]}]}),

    panel("barchart", "Daily energy by source (kWh, all shops)", 0, 22, 24, 8, [target("""
SELECT to_char(time_bucket('1 day', bucket, 'Africa/Kigali'), 'MM-DD') AS day,
       round(coalesce(sum(kwh) FILTER (WHERE source = 'GRID'), 0)::numeric, 1) AS "Grid",
       round(coalesce(sum(kwh) FILTER (WHERE source = 'GENERATOR'), 0)::numeric, 1) AS "Generator"
FROM energy_hourly e JOIN devices d USING (device_id)
WHERE d.role = 'shop' AND $__timeFilter(bucket)
GROUP BY 1 ORDER BY 1""")],
          options={"stacking": "normal", "xField": "day", "showValue": "never"},
          fieldConfig={"defaults": {"unit": "kwatth"}, "overrides": [
              {"matcher": {"id": "byName", "options": "Grid"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}}]},
              {"matcher": {"id": "byName", "options": "Generator"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]}]}),

    panel("timeseries", "Power per shop (kW)", 0, 30, 24, 8, [target("""
SELECT $__timeGroupAlias(ts, '1m'), d.name AS metric, avg(p_kw) AS kw
FROM readings r JOIN devices d USING (device_id)
WHERE $__timeFilter(ts) AND d.device_id IN ($shop)
GROUP BY 1, 2 ORDER BY 1""", fmt="time_series")],
          fieldConfig={"defaults": {"unit": "kwatt"}, "overrides": []}),

    panel("table", "Outages and generator runs", 0, 38, 12, 9, [target("""
SELECT started AS "Start", ended AS "End", source AS "Supply",
       to_char(duration, 'HH24:MI:SS') AS "Duration"
FROM source_periods WHERE source <> 'GRID' AND $__timeFilter(started)
ORDER BY started DESC""")],
          fieldConfig={"defaults": {}, "overrides": [
              {"matcher": {"id": "byName", "options": "Supply"},
               "properties": [{"id": "mappings", "value": SOURCE_MAP}, {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}]}),

    panel("table", "Night use, last night 23:00-05:00 (kWh)", 12, 38, 12, 9, [target("""
WITH win AS (
  SELECT (date_trunc('day', now() AT TIME ZONE 'Africa/Kigali') - interval '1 hour') AT TIME ZONE 'Africa/Kigali' AS s,
         (date_trunc('day', now() AT TIME ZONE 'Africa/Kigali') + interval '5 hour') AT TIME ZONE 'Africa/Kigali' AS e)
SELECT d.name AS "Shop", round(coalesce(sum(r.d_kwh), 0)::numeric, 2) AS "kWh",
       round(coalesce(avg(r.p_kw), 0)::numeric, 2) AS "Avg kW"
FROM devices d LEFT JOIN readings r ON r.device_id = d.device_id
  AND r.ts >= (SELECT s FROM win) AND r.ts < (SELECT e FROM win)
WHERE d.role = 'shop' GROUP BY d.name ORDER BY 2 DESC""")]),

    panel("table", "Device and gateway events (last 7 days)", 0, 47, 24, 8, [target("""
SELECT ts AS "Time", gateway AS "Gateway", type AS "Event",
       coalesce(data->>'dev', data->>'new', '') AS "Device / supply", data::text AS "Details"
FROM events WHERE type IN ('device_offline', 'device_online', 'source_change')
  AND ts > now() - interval '7 days' ORDER BY ts DESC LIMIT 200""")]),
]

dashboard = {
    "uid": "kigali-building-energy",
    "title": "Building energy: grid, generator and shops",
    "tags": ["energy", "kigali"],
    "timezone": "Africa/Kigali",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {"from": "now-24h", "to": "now"},
    "templating": {"list": [{
        "name": "shop", "label": "Shop", "type": "query", "datasource": DS,
        "query": "SELECT name AS __text, device_id AS __value FROM devices WHERE role = 'shop' ORDER BY name",
        "definition": "SELECT name AS __text, device_id AS __value FROM devices WHERE role = 'shop' ORDER BY name",
        "multi": True, "includeAll": True, "current": {"text": "All", "value": "$__all"}, "refresh": 1}]},
    "panels": panels,
}


def rule(uid, title, sql, op, value, for_="5m", summary=""):
    return {
        "uid": uid, "title": title, "condition": "C", "for": for_, "noDataState": "OK", "execErrState": "Error",
        "labels": {"team": "building"}, "annotations": {"summary": summary},
        "data": [
            {"refId": "A", "relativeTimeRange": {"from": 600, "to": 0}, "datasourceUid": "energy-db",
             "model": target(sql)},
            {"refId": "C", "datasourceUid": "__expr__",
             "model": {"refId": "C", "type": "threshold", "expression": "A",
                       "conditions": [{"evaluator": {"type": op, "params": [value]}}]}},
        ],
    }


NOT_DARK = "(SELECT data->>'new' FROM events WHERE type = 'source_change' ORDER BY ts DESC LIMIT 1) <> 'NONE'"
rules = {"apiVersion": 1, "groups": [{
    "orgId": 1, "name": "building-energy", "folder": "Energy", "interval": "1m",
    "rules": [
        rule("meter-offline", "Shop meter offline", f"""
SELECT d.name AS shop, coalesce(extract(epoch FROM now() - max(r.ts)) / 60, 1440) AS minutes_silent
FROM devices d LEFT JOIN readings r ON r.device_id = d.device_id AND r.ts > now() - interval '1 day'
WHERE d.role = 'shop' AND d.active AND {NOT_DARK} GROUP BY d.name""", "gt", 5,
             summary="A shop meter has sent nothing for over 5 minutes while the building has power."),
        rule("shop-overload", "Shop above contracted kW", """
SELECT name AS shop, p_kw / nullif(contracted_kw, 0) AS load_ratio
FROM latest_readings WHERE role = 'shop' AND contracted_kw > 0""", "gt", 1.0,
             summary="A shop is drawing more than its contracted power."),
        rule("night-use", "Unusual night use", """
SELECT name AS shop, p_kw / nullif(contracted_kw, 0) AS night_ratio
FROM latest_readings
WHERE role = 'shop' AND contracted_kw > 0
  AND (extract(hour FROM now() AT TIME ZONE 'Africa/Kigali') >= 23 OR extract(hour FROM now() AT TIME ZONE 'Africa/Kigali') < 5)""",
             "gt", 0.25, for_="30m", summary="A shop is using over 25% of its contracted power at night."),
        rule("fuel-low", "Generator fuel low", """
SELECT (m->>'fuel_pct')::float AS fuel_pct FROM readings r JOIN devices d USING (device_id)
WHERE d.role = 'genset' AND m ? 'fuel_pct' ORDER BY ts DESC LIMIT 1""", "lt", 20, for_="2m",
             summary="Generator fuel below 20%."),
        rule("gateway-silent", "Gateway not reporting", """
SELECT gateway, extract(epoch FROM now() - max(received)) / 60 AS minutes_silent FROM batches GROUP BY gateway""",
             "gt", 10, for_="1m", summary="No data from the Pi gateway for 10 minutes (internet, power or Pi problem)."),
    ]}]}

with open(os.path.join(ROOT, "grafana/dashboards/building.json"), "w") as f:
    json.dump(dashboard, f, indent=2)
with open(os.path.join(ROOT, "grafana/provisioning/alerting/rules.yaml"), "w") as f:
    f.write("# Generated by scripts/build_dashboard.py. Add a contact point (email, Telegram, SMS) in Grafana.\n")
    class NoAlias(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True
    yaml.dump(rules, f, Dumper=NoAlias, sort_keys=False, width=200)
print("wrote dashboard with", len(panels), "panels and", len(rules["groups"][0]["rules"]), "alert rules")
