-- BreatheSense analytical schema (PostgreSQL / Neon).
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS). Never drops anything.
-- Design notes, driven by Phase 1 findings:
--   * dim_location.gas_unit_convention  (per-station real unit of NO2/SO2/CO)
--   * dim_sensor.datetime_first/last, is_live  (legacy vs current sensors, cohort selection)

CREATE TABLE IF NOT EXISTS dim_location (
    location_id         INT PRIMARY KEY,              -- OpenAQ location id
    location_name       TEXT NOT NULL,
    city                TEXT,                         -- NULL = unmapped (excluded from city rollups)
    state               TEXT,
    latitude            NUMERIC(9,6),
    longitude           NUMERIC(9,6),
    provider            TEXT,
    owner               TEXT,
    timezone            TEXT,
    is_monitor          BOOLEAN,
    is_mobile           BOOLEAN,
    is_active           BOOLEAN DEFAULT TRUE,         -- observation within the last 7 days
    gas_unit_convention TEXT NOT NULL DEFAULT 'unclassified'
                        CHECK (gas_unit_convention IN ('ugm3', 'ppb', 'unclassified')),
    datetime_first      TIMESTAMPTZ,
    datetime_last       TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dim_sensor (
    sensor_id      INT PRIMARY KEY,                   -- OpenAQ sensor id
    location_id    INT NOT NULL REFERENCES dim_location(location_id),
    parameter      TEXT NOT NULL,                     -- pm25, pm10, no2, so2, o3, co, no, nox, ...
    source_units   TEXT,                              -- unit label as published by OpenAQ
    datetime_first TIMESTAMPTZ,
    datetime_last  TIMESTAMPTZ,                       -- sensor-level, from /locations/{id}/latest
    is_live        BOOLEAN NOT NULL DEFAULT FALSE,    -- the sensor we ingest for (location, parameter)
    updated_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dim_sensor_location ON dim_sensor (location_id);
-- At most one live sensor per (location, parameter).
CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_sensor_live
    ON dim_sensor (location_id, parameter) WHERE is_live;

-- Hourly pollutant concentrations in standard units (ug/m3; CO mg/m3).
-- reading_time = OpenAQ period.datetimeFrom in UTC (CPCB hours start at HH:30Z = whole IST hours).
CREATE TABLE IF NOT EXISTS fact_pollutant_hourly (
    location_id  INT NOT NULL REFERENCES dim_location(location_id),
    reading_time TIMESTAMPTZ NOT NULL,
    pm25 NUMERIC, pm10 NUMERIC, no2 NUMERIC, so2 NUMERIC,
    co   NUMERIC, o3   NUMERIC, nh3 NUMERIC, pb  NUMERIC,
    valid_observation_count INT,
    quality_flag TEXT,
    loaded_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (location_id, reading_time)
);
CREATE INDEX IF NOT EXISTS idx_fact_pollutant_hourly_time ON fact_pollutant_hourly (reading_time);

CREATE TABLE IF NOT EXISTS ref_aqi_breakpoints (
    parameter TEXT NOT NULL,
    avg_hours INT  NOT NULL,
    conc_low  NUMERIC NOT NULL,
    conc_high NUMERIC,                                -- NULL = open-ended top band
    aqi_low   INT NOT NULL,
    aqi_high  INT NOT NULL,
    PRIMARY KEY (parameter, aqi_low)
);

CREATE TABLE IF NOT EXISTS daily_city_summary (
    city  TEXT NOT NULL,
    state TEXT NOT NULL,
    summary_date DATE NOT NULL,                       -- IST calendar day
    avg_pm25 NUMERIC, avg_pm10 NUMERIC, avg_no2 NUMERIC, avg_so2 NUMERIC,
    avg_co NUMERIC, avg_o3 NUMERIC, avg_nh3 NUMERIC, avg_pb NUMERIC,
    median_station_aqi NUMERIC,
    max_station_aqi    INT,
    dominant_pollutant TEXT,
    max_hourly_pm25    NUMERIC,                       -- NULL for backfill rows
    hours_poor_or_worse INT,                          -- NULL for backfill rows
    reporting_locations INT,
    source TEXT NOT NULL CHECK (source IN ('live_rollup', 'backfill')),
    PRIMARY KEY (city, state, summary_date)
);

-- Phase 7: backfill rows use daily-mean O3/CO instead of the max 8 h mean.
ALTER TABLE daily_city_summary ADD COLUMN IF NOT EXISTS o3_co_approximated BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS dim_date (
    date DATE PRIMARY KEY,
    year INT, month INT, month_name TEXT, day_of_week TEXT,
    season TEXT,                  -- Winter Dec-Feb, Summer Mar-May, Monsoon Jun-Sep, Post-monsoon Oct-Nov
    is_festival BOOLEAN DEFAULT FALSE,
    festival_name TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_log (
    run_id SERIAL PRIMARY KEY,
    job TEXT, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    api_requests INT, rows_fetched INT, rows_valid INT, rows_invalid INT,
    rows_upserted INT, fresh_locations INT, stale_locations INT,
    status TEXT, error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipeline_log_started ON pipeline_log (started_at);
-- Phase 4: data-quality metrics that have no column above.
ALTER TABLE pipeline_log ADD COLUMN IF NOT EXISTS duplicates INT;
ALTER TABLE pipeline_log ADD COLUMN IF NOT EXISTS details JSONB;  -- invalid_by_reason, cohort, fetch errors, ...

CREATE TABLE IF NOT EXISTS backfill_progress (
    sensor_id INT PRIMARY KEY,
    last_date_loaded DATE,
    status TEXT,
    updated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS alert_log (                -- station-level alerts
    location_id INT REFERENCES dim_location(location_id),
    alerted_at  TIMESTAMPTZ DEFAULT now(),
    aqi INT,
    PRIMARY KEY (location_id, alerted_at)
);
