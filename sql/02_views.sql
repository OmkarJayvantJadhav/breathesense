-- BreatheSense analytical layer. Idempotent: CREATE OR REPLACE.
-- Times are shown in IST for Power BI; keys stay in UTC.
--
-- AQI logic lives in SQL functions that read ref_aqi_breakpoints, the same CSV
-- src/aqi.py reads. Rules mirror src/aqi.py exactly (tests/integration checks it):
--   * round concentration to table precision (CO, Pb: 1 decimal; others: integer);
--     ROUND(numeric) is half away from zero = Python ROUND_HALF_UP for C >= 0
--   * first band with C <= conc_high; interpolate; clamp to the band's AQI range
--   * open-ended top band: previous band's slope from aqi_low, capped at 500
--   * AQI valid with >= 3 sub-indices including PM2.5 or PM10; dominant = max
--     sub-index, ties broken pm25, pm10, no2, o3, co, so2, nh3, pb
-- Thresholds come from ref_settings (loaded by scripts/init_db.py from .env).

CREATE TABLE IF NOT EXISTS ref_settings (
    key   TEXT PRIMARY KEY,
    value NUMERIC NOT NULL
);

-- ------------------------------------------------------------------ functions
CREATE OR REPLACE FUNCTION fn_setting(p_key TEXT) RETURNS NUMERIC
LANGUAGE sql STABLE AS $$
    SELECT value FROM ref_settings WHERE key = p_key
$$;

CREATE OR REPLACE FUNCTION fn_sub_index(p_param TEXT, p_conc NUMERIC) RETURNS NUMERIC
LANGUAGE sql STABLE AS $$
    WITH c AS (
        SELECT ROUND(p_conc, CASE WHEN p_param IN ('co', 'pb') THEN 1 ELSE 0 END) AS v
        WHERE p_conc IS NOT NULL AND p_conc >= 0
    ),
    bands AS (
        SELECT b.*, LAG(b.conc_low)  OVER w AS prev_low,  LAG(b.conc_high) OVER w AS prev_high,
                    LAG(b.aqi_low)   OVER w AS prev_alow, LAG(b.aqi_high)  OVER w AS prev_ahigh
        FROM ref_aqi_breakpoints b
        WHERE b.parameter = p_param
        WINDOW w AS (ORDER BY b.aqi_low)
    ),
    hit AS (
        SELECT bands.*, c.v FROM bands, c
        WHERE bands.conc_high IS NULL OR c.v <= bands.conc_high
        ORDER BY bands.aqi_low
        LIMIT 1
    )
    SELECT CASE
        WHEN conc_high IS NULL THEN
            LEAST(500, GREATEST(aqi_low,
                aqi_low + (prev_ahigh - prev_alow) / (prev_high - prev_low) * (v - conc_low)))
        ELSE
            LEAST(aqi_high, GREATEST(aqi_low,
                (aqi_high - aqi_low) / (conc_high - conc_low) * (v - conc_low) + aqi_low))
    END
    FROM hit
$$;

CREATE OR REPLACE FUNCTION fn_aqi_category(p_aqi NUMERIC) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_aqi IS NULL THEN NULL
        WHEN p_aqi <= 50  THEN 'Good'
        WHEN p_aqi <= 100 THEN 'Satisfactory'
        WHEN p_aqi <= 200 THEN 'Moderate'
        WHEN p_aqi <= 300 THEN 'Poor'
        WHEN p_aqi <= 400 THEN 'Very Poor'
        ELSE 'Severe'
    END
$$;

CREATE OR REPLACE FUNCTION fn_aqi_color(p_aqi NUMERIC) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_aqi IS NULL THEN NULL
        WHEN p_aqi <= 50  THEN '#00B050'
        WHEN p_aqi <= 100 THEN '#92D050'
        WHEN p_aqi <= 200 THEN '#FFFF00'
        WHEN p_aqi <= 300 THEN '#FF9900'
        WHEN p_aqi <= 400 THEN '#FF0000'
        ELSE '#C00000'
    END
$$;

-- Half-up rounding for positive values (AQI is never negative).
CREATE OR REPLACE FUNCTION fn_round_aqi(p NUMERIC) RETURNS INT
LANGUAGE sql IMMUTABLE AS $$ SELECT ROUND(p)::INT $$;

-- ------------------------------------------------------------------ station AQI
-- One row per active station, as of its own newest stored hour. is_fresh says
-- whether that hour is recent (fresh_hours); stale stations keep their last AQI
-- but must not be presented as current.
CREATE OR REPLACE VIEW vw_location_aqi_now AS
WITH latest AS (
    SELECT location_id, max(reading_time) AS as_of
    FROM fact_pollutant_hourly GROUP BY location_id
),
win AS (
    SELECT f.location_id, l.as_of,
        avg(f.pm25) FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS pm25_24h,
        count(f.pm25) FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS pm25_n,
        avg(f.pm10) FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS pm10_24h,
        count(f.pm10) FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS pm10_n,
        avg(f.no2)  FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS no2_24h,
        count(f.no2)  FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS no2_n,
        avg(f.so2)  FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS so2_24h,
        count(f.so2)  FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS so2_n,
        avg(f.nh3)  FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS nh3_24h,
        count(f.nh3)  FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS nh3_n,
        avg(f.pb)   FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS pb_24h,
        count(f.pb)   FILTER (WHERE f.reading_time > l.as_of - interval '24 hours') AS pb_n,
        avg(f.o3)   FILTER (WHERE f.reading_time > l.as_of - interval '8 hours')  AS o3_8h,
        count(f.o3)   FILTER (WHERE f.reading_time > l.as_of - interval '8 hours')  AS o3_n,
        avg(f.co)   FILTER (WHERE f.reading_time > l.as_of - interval '8 hours')  AS co_8h,
        count(f.co)   FILTER (WHERE f.reading_time > l.as_of - interval '8 hours')  AS co_n
    FROM fact_pollutant_hourly f
    JOIN latest l USING (location_id)
    WHERE f.reading_time > l.as_of - interval '24 hours'
    GROUP BY f.location_id, l.as_of
),
subs AS (
    SELECT w.*,
        fn_sub_index('pm25', CASE WHEN pm25_n >= fn_setting('min_hours_24h') THEN pm25_24h END) AS si_pm25,
        fn_sub_index('pm10', CASE WHEN pm10_n >= fn_setting('min_hours_24h') THEN pm10_24h END) AS si_pm10,
        fn_sub_index('no2',  CASE WHEN no2_n  >= fn_setting('min_hours_24h') THEN no2_24h  END) AS si_no2,
        fn_sub_index('so2',  CASE WHEN so2_n  >= fn_setting('min_hours_24h') THEN so2_24h  END) AS si_so2,
        fn_sub_index('nh3',  CASE WHEN nh3_n  >= fn_setting('min_hours_24h') THEN nh3_24h  END) AS si_nh3,
        fn_sub_index('pb',   CASE WHEN pb_n   >= fn_setting('min_hours_24h') THEN pb_24h   END) AS si_pb,
        fn_sub_index('o3',   CASE WHEN o3_n   >= fn_setting('min_hours_8h')  THEN o3_8h    END) AS si_o3,
        fn_sub_index('co',   CASE WHEN co_n   >= fn_setting('min_hours_8h')  THEN co_8h    END) AS si_co
    FROM win w
),
scored AS (
    SELECT s.*,
        num_nonnulls(si_pm25, si_pm10, si_no2, si_so2, si_nh3, si_pb, si_o3, si_co) AS n_sub,
        GREATEST(si_pm25, si_pm10, si_no2, si_so2, si_nh3, si_pb, si_o3, si_co) AS max_sub
    FROM subs s
),
valid AS (
    SELECT sc.*,
        (n_sub >= 3 AND (si_pm25 IS NOT NULL OR si_pm10 IS NOT NULL)) AS aqi_valid
    FROM scored sc
)
SELECT
    v.location_id                                   AS "Location ID",
    l.location_name                                 AS "Station",
    l.city                                          AS "City",
    l.state                                         AS "State",
    l.latitude                                      AS "Latitude",
    l.longitude                                     AS "Longitude",
    (v.as_of AT TIME ZONE 'Asia/Kolkata')           AS "Last Observed (IST)",
    v.as_of                                         AS last_observed_utc,
    round(extract(epoch FROM now() - v.as_of) / 3600.0, 1) AS "Hours Since Last Observation",
    (v.as_of >= now() - make_interval(hours => fn_setting('fresh_hours')::int)) AS "Is Fresh",
    round(v.pm25_24h, 1) AS "PM2.5 24h Avg", v.pm25_n AS "PM2.5 Hours",
    round(v.pm10_24h, 1) AS "PM10 24h Avg",  v.pm10_n AS "PM10 Hours",
    round(v.no2_24h, 1)  AS "NO2 24h Avg",   v.no2_n  AS "NO2 Hours",
    round(v.so2_24h, 1)  AS "SO2 24h Avg",   v.so2_n  AS "SO2 Hours",
    round(v.o3_8h, 1)    AS "O3 8h Avg",     v.o3_n   AS "O3 Hours (8h)",
    round(v.co_8h, 2)    AS "CO 8h Avg",     v.co_n   AS "CO Hours (8h)",
    round(v.si_pm25) AS "PM2.5 Sub-index", round(v.si_pm10) AS "PM10 Sub-index",
    round(v.si_no2)  AS "NO2 Sub-index",   round(v.si_so2)  AS "SO2 Sub-index",
    round(v.si_o3)   AS "O3 Sub-index",    round(v.si_co)   AS "CO Sub-index",
    v.n_sub                                          AS "Pollutants With Complete Windows",
    CASE WHEN v.aqi_valid THEN fn_round_aqi(v.max_sub) END                       AS "BreatheSense AQI",
    CASE WHEN v.aqi_valid THEN fn_aqi_category(fn_round_aqi(v.max_sub)) END      AS "AQI Category",
    CASE WHEN v.aqi_valid THEN fn_aqi_color(fn_round_aqi(v.max_sub)) END         AS "AQI Color",
    CASE WHEN v.aqi_valid THEN (
        SELECT p FROM (VALUES (1,'pm25',v.si_pm25),(2,'pm10',v.si_pm10),(3,'no2',v.si_no2),(4,'o3',v.si_o3),
                              (5,'co',v.si_co),(6,'so2',v.si_so2),(7,'nh3',v.si_nh3),(8,'pb',v.si_pb)) t(o, p, s)
        WHERE s = v.max_sub ORDER BY o LIMIT 1) END   AS "Dominant Pollutant",
    CASE WHEN v.aqi_valid THEN NULL
         WHEN v.n_sub < 3 THEN 'fewer_than_3_pollutants' ELSE 'no_pm' END  AS "AQI Invalid Reason",
    l.gas_unit_convention                           AS "Gas Unit Convention",
    l.provider                                      AS "Provider"
FROM valid v
JOIN dim_location l USING (location_id)
WHERE l.is_active;

-- ------------------------------------------------------------------ city rollup (fresh stations)
CREATE OR REPLACE VIEW vw_city_latest AS
SELECT
    "City", "State",
    percentile_cont(0.5) WITHIN GROUP (ORDER BY "BreatheSense AQI")          AS "Median Station AQI",
    max("BreatheSense AQI")                                                   AS "Max Station AQI",
    count(*) FILTER (WHERE "BreatheSense AQI" IS NOT NULL)                    AS "Reporting Stations",
    count(*) FILTER (WHERE "BreatheSense AQI" >= fn_setting('aqi_alert_threshold')) AS "Alerting Stations",
    mode() WITHIN GROUP (ORDER BY "Dominant Pollutant")                       AS "Most Frequent Dominant Pollutant",
    max("Last Observed (IST)")                                                AS "Latest Observation (IST)",
    'analytical rollup of station AQIs, not an official CPCB city AQI'::text  AS "Note"
FROM vw_location_aqi_now
WHERE "Is Fresh" AND "City" IS NOT NULL
GROUP BY "City", "State";

-- ------------------------------------------------------------------ last 24 h, hourly, per city
CREATE OR REPLACE VIEW vw_last_24h AS
SELECT
    l.city AS "City", l.state AS "State",
    (f.reading_time AT TIME ZONE 'Asia/Kolkata') AS "Hour (IST)",
    f.reading_time                               AS hour_utc,
    round(avg(f.pm25), 1) AS "PM2.5", round(avg(f.pm10), 1) AS "PM10",
    round(avg(f.no2), 1)  AS "NO2",   round(avg(f.so2), 1)  AS "SO2",
    round(avg(f.o3), 1)   AS "O3",    round(avg(f.co), 2)   AS "CO",
    count(DISTINCT f.location_id) AS "Stations Reporting"
FROM fact_pollutant_hourly f
JOIN dim_location l USING (location_id)
WHERE f.reading_time >= now() - interval '24 hours' AND l.city IS NOT NULL
GROUP BY l.city, l.state, f.reading_time;

-- ------------------------------------------------------------------ alerts
CREATE OR REPLACE VIEW vw_active_alerts AS
SELECT "Location ID", "Station", "City", "State", "BreatheSense AQI", "AQI Category",
       "Dominant Pollutant", "Last Observed (IST)", last_observed_utc
FROM vw_location_aqi_now
WHERE "Is Fresh" AND "BreatheSense AQI" >= fn_setting('aqi_alert_threshold');

-- ------------------------------------------------------------------ daily history
CREATE OR REPLACE VIEW vw_city_daily_all AS
SELECT
    s.city AS "City", s.state AS "State", s.summary_date AS "Date",
    d.year AS "Year", d.month AS "Month", d.month_name AS "Month Name",
    d.day_of_week AS "Day of Week", d.season AS "Season",
    d.is_festival AS "Is Festival", d.festival_name AS "Festival",
    s.avg_pm25 AS "Avg PM2.5", s.avg_pm10 AS "Avg PM10", s.avg_no2 AS "Avg NO2",
    s.avg_so2 AS "Avg SO2", s.avg_o3 AS "Avg O3", s.avg_co AS "Avg CO",
    s.median_station_aqi AS "Median Station AQI", s.max_station_aqi AS "Max Station AQI",
    fn_aqi_category(fn_round_aqi(s.median_station_aqi)) AS "Median AQI Category",
    s.dominant_pollutant AS "Dominant Pollutant",
    s.max_hourly_pm25 AS "Max Hourly PM2.5", s.hours_poor_or_worse AS "Hours Poor or Worse",
    s.reporting_locations AS "Reporting Stations", s.source AS "Source",
    s.o3_co_approximated AS "O3/CO Daily-Mean Approximation"
FROM daily_city_summary s
JOIN dim_date d ON d.date = s.summary_date;

-- ------------------------------------------------------------------ data quality per station
CREATE OR REPLACE VIEW vw_data_quality AS
WITH last24 AS (
    SELECT location_id,
        count(*)                AS hours_with_data,
        count(pm25) AS pm25_h, count(pm10) AS pm10_h, count(no2) AS no2_h,
        count(so2)  AS so2_h,  count(o3)   AS o3_h,   count(co)  AS co_h,
        count(*) FILTER (WHERE quality_flag LIKE '%low_coverage%') AS low_coverage_h
    FROM fact_pollutant_hourly
    WHERE reading_time >= now() - interval '24 hours'
    GROUP BY location_id
),
latest AS (
    SELECT location_id, max(reading_time) AS last_hour FROM fact_pollutant_hourly GROUP BY location_id
),
live AS (
    SELECT location_id, array_agg(parameter ORDER BY parameter) AS live_params
    FROM dim_sensor WHERE is_live AND parameter IN ('pm25','pm10','no2','so2','o3','co','nh3','pb')
    GROUP BY location_id
)
SELECT
    l.location_id AS "Location ID", l.location_name AS "Station", l.city AS "City", l.state AS "State",
    l.provider AS "Provider", l.gas_unit_convention AS "Gas Unit Convention",
    (lt.last_hour AT TIME ZONE 'Asia/Kolkata') AS "Last Stored Hour (IST)",
    round(extract(epoch FROM now() - lt.last_hour) / 3600.0, 1) AS "Hours Since Last Data",
    coalesce(lt.last_hour >= now() - make_interval(hours => fn_setting('fresh_hours')::int), FALSE) AS "Is Fresh",
    coalesce(q.hours_with_data, 0) AS "Hours With Data (24h)",
    round(100.0 * coalesce(q.hours_with_data, 0) / 24, 1) AS "Completeness % (24h)",
    coalesce(q.low_coverage_h, 0) AS "Low Coverage Hours (24h)",
    array_to_string(lv.live_params, ', ') AS "Live Pollutant Sensors",
    array_to_string(ARRAY(
        SELECT p FROM unnest(ARRAY['pm25','pm10','no2','so2','o3','co']) p
        WHERE CASE p WHEN 'pm25' THEN coalesce(q.pm25_h,0) WHEN 'pm10' THEN coalesce(q.pm10_h,0)
                     WHEN 'no2' THEN coalesce(q.no2_h,0)   WHEN 'so2' THEN coalesce(q.so2_h,0)
                     WHEN 'o3' THEN coalesce(q.o3_h,0)     ELSE coalesce(q.co_h,0) END = 0), ', ')
        AS "Pollutants Missing (24h)"
FROM dim_location l
LEFT JOIN latest lt USING (location_id)
LEFT JOIN last24 q  USING (location_id)
LEFT JOIN live lv   USING (location_id)
WHERE l.is_active;

-- ------------------------------------------------------------------ pipeline health
CREATE OR REPLACE VIEW vw_pipeline_health AS
WITH hourly AS (
    SELECT * FROM pipeline_log WHERE job = 'hourly_etl'
),
last_ok AS (
    SELECT * FROM hourly WHERE status IN ('success', 'partial') ORDER BY started_at DESC LIMIT 1
),
last_run AS (
    SELECT * FROM hourly ORDER BY started_at DESC LIMIT 1
)
SELECT
    (SELECT finished_at AT TIME ZONE 'Asia/Kolkata' FROM last_ok)          AS "Last Successful Run (IST)",
    (SELECT round(extract(epoch FROM now() - finished_at) / 60) FROM last_ok) AS "Minutes Since Last Successful Run",
    (SELECT status FROM last_run)                                           AS "Last Run Status",
    (SELECT (details ->> 'cohort')::int FROM last_run)                      AS "Last Run Cohort",
    (SELECT api_requests FROM last_run)                                     AS "Last Run API Requests",
    (SELECT rows_fetched FROM last_run)                                     AS "Last Run Observations",
    (SELECT rows_invalid FROM last_run)                                     AS "Last Run Invalid",
    (SELECT rows_upserted FROM last_run)                                    AS "Last Run Rows Upserted",
    (SELECT round(extract(epoch FROM now() - max(loaded_at)) / 60) FROM fact_pollutant_hourly) AS "Minutes Since Last Load",
    (SELECT max(reading_time) AT TIME ZONE 'Asia/Kolkata' FROM fact_pollutant_hourly) AS "Newest Reading (IST)",
    (SELECT count(*) FROM hourly WHERE started_at >= now() - interval '24 hours')   AS "Runs (24h)",
    (SELECT count(*) FROM hourly WHERE started_at >= now() - interval '24 hours'
                                  AND status = 'failed')                           AS "Failed Runs (24h)",
    (SELECT coalesce(sum(api_requests), 0) FROM pipeline_log
      WHERE started_at >= now() - interval '24 hours')                             AS "API Requests (24h)",
    (SELECT count(*) FILTER (WHERE "Is Fresh") FROM vw_data_quality)               AS "Fresh Stations",
    (SELECT count(*) FILTER (WHERE NOT "Is Fresh") FROM vw_data_quality)           AS "Stale Stations",
    round(pg_database_size(current_database()) / 1024.0 / 1024.0, 1)              AS "DB Size MB";
