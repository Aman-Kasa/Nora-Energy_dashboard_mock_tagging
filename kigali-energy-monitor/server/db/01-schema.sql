-- TimescaleDB schema for the Kigali building energy monitor.
-- Loaded automatically on first start of the database container.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Every meter / controller known to the system. Filled automatically from the
-- gateway's "devices" event; tenant details can be edited here or in Grafana.
CREATE TABLE IF NOT EXISTS devices (
    device_id      TEXT PRIMARY KEY,
    site           TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN ('main','generator_meter','genset','shop')),
    name           TEXT,
    tenant         TEXT,
    phone          TEXT,                 -- for SMS / MoMo bills
    floor          INT,
    area_m2        NUMERIC,
    contracted_kw  NUMERIC,
    model          TEXT,
    unit           INT,
    tank_litres    NUMERIC,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raw readings. source = which supply fed the building during the interval
-- that ends at ts (GRID / GENERATOR / NONE). d_kwh = energy in that interval.
CREATE TABLE IF NOT EXISTS readings (
    ts         TIMESTAMPTZ NOT NULL,
    site       TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    source     TEXT NOT NULL,
    p_kw       DOUBLE PRECISION,
    v          DOUBLE PRECISION,
    i          DOUBLE PRECISION,
    pf         DOUBLE PRECISION,
    hz         DOUBLE PRECISION,
    kwh        DOUBLE PRECISION,
    d_kwh      DOUBLE PRECISION,
    m          JSONB,                    -- everything else (per-phase values, generator data)
    PRIMARY KEY (device_id, ts)          -- makes re-sent batches harmless
);
SELECT create_hypertable('readings', 'ts', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS readings_site_ts ON readings (site, ts DESC);

ALTER TABLE readings SET (timescaledb.compress, timescaledb.compress_segmentby = 'device_id');
SELECT add_compression_policy('readings', INTERVAL '14 days', if_not_exists => TRUE);
-- Raw 10-second data kept 2 years; hourly aggregates (below) are kept for ever.
SELECT add_retention_policy('readings', INTERVAL '730 days', if_not_exists => TRUE);

-- Events: source changes, devices offline/online, heartbeats, device lists.
CREATE TABLE IF NOT EXISTS events (
    ts       TIMESTAMPTZ NOT NULL,
    site     TEXT NOT NULL,
    gateway  TEXT NOT NULL,
    type     TEXT NOT NULL,
    data     JSONB,
    PRIMARY KEY (gateway, ts, type)
);
CREATE INDEX IF NOT EXISTS events_type_ts ON events (type, ts DESC);

-- Batches received, to spot gaps or replays in the sequence numbers.
CREATE TABLE IF NOT EXISTS batches (
    gateway   TEXT NOT NULL,
    seq       BIGINT NOT NULL,
    kind      TEXT NOT NULL,
    items     INT NOT NULL,
    received  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (gateway, seq)
);

-- Hourly energy per device and source: the basis of dashboards and bills.
CREATE MATERIALIZED VIEW IF NOT EXISTS energy_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', ts) AS bucket,
       site, device_id, source,
       sum(d_kwh)  AS kwh,
       avg(p_kw)   AS avg_kw,
       max(p_kw)   AS max_kw
FROM readings
GROUP BY bucket, site, device_id, source
WITH NO DATA;
SELECT add_continuous_aggregate_policy('energy_hourly',
    start_offset => INTERVAL '3 days', end_offset => INTERVAL '10 minutes',
    schedule_interval => INTERVAL '10 minutes', if_not_exists => TRUE);
ALTER MATERIALIZED VIEW energy_hourly SET (timescaledb.materialized_only = false);

-- Outages / generator periods built from source_change events.
CREATE OR REPLACE VIEW source_periods AS
SELECT site,
       data->>'new'                                   AS source,
       ts                                             AS started,
       lead(ts) OVER (PARTITION BY site ORDER BY ts)  AS ended,
       coalesce(lead(ts) OVER (PARTITION BY site ORDER BY ts), now()) - ts AS duration
FROM events
WHERE type = 'source_change';

-- Latest reading per device (live tables in Grafana).
CREATE OR REPLACE VIEW latest_readings AS
SELECT DISTINCT ON (r.device_id)
       r.device_id, d.name, d.role, d.floor, d.contracted_kw, r.ts, r.source, r.p_kw, r.v, r.i, r.pf, r.kwh, r.m
FROM readings r
JOIN devices d USING (device_id)
WHERE r.ts > now() - INTERVAL '1 day'
ORDER BY r.device_id, r.ts DESC;

-- Tariff inputs entered per month (EUCL bill, fuel bought, etc.).
CREATE TABLE IF NOT EXISTS month_inputs (
    site            TEXT NOT NULL,
    month           DATE NOT NULL,            -- first day of month
    eucl_bill_rwf   NUMERIC,                  -- the building's real grid bill, VAT included
    fuel_litres     NUMERIC,                  -- if known from refuel receipts
    diesel_rwf_l    NUMERIC,
    notes           TEXT,
    PRIMARY KEY (site, month)
);

CREATE TABLE IF NOT EXISTS bills (
    site            TEXT NOT NULL,
    month           DATE NOT NULL,
    device_id       TEXT NOT NULL,
    shop_name       TEXT,
    grid_kwh        NUMERIC,
    gen_kwh         NUMERIC,
    common_kwh      NUMERIC,
    grid_rwf        NUMERIC,
    gen_rwf         NUMERIC,
    common_rwf      NUMERIC,
    total_rwf       NUMERIC,
    run_id          INT,
    PRIMARY KEY (site, month, device_id)
);

CREATE TABLE IF NOT EXISTS billing_runs (
    run_id      SERIAL PRIMARY KEY,
    site        TEXT NOT NULL,
    month       DATE NOT NULL,
    run_by      TEXT NOT NULL,
    run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    inputs      JSONB,
    summary     JSONB
);
