-- BreatheSense analysis queries.
-- Each query states the business question and the SQL concept it demonstrates.
-- City keys are always (city, state): city names repeat across states.
-- Daily history comes from daily_city_summary (live rollups + Phase 7 backfill);
-- "station AQI" means the BreatheSense AQI (CPCB method), not an official CPCB value.


-- 1. How are station AQIs distributed within each city right now?
--    Concept: ordered-set aggregates (percentile_cont) over a view.
SELECT "City", "State",
       count(*)                                                            AS stations,
       percentile_cont(0.10) WITHIN GROUP (ORDER BY "BreatheSense AQI")    AS p10_aqi,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY "BreatheSense AQI")    AS median_aqi,
       percentile_cont(0.90) WITHIN GROUP (ORDER BY "BreatheSense AQI")    AS p90_aqi,
       max("BreatheSense AQI")                                             AS max_aqi
FROM vw_location_aqi_now
WHERE "BreatheSense AQI" IS NOT NULL AND "City" IS NOT NULL
GROUP BY "City", "State"
HAVING count(*) >= 3
ORDER BY median_aqi DESC;


-- 2. What is the 7-day rolling PM2.5 trend per city?
--    Concept: window frame (ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) per partition.
--    The rolling value is only shown once 7 days are in the frame.
SELECT city, state, summary_date, avg_pm25,
       CASE WHEN count(avg_pm25) OVER w = 7 THEN round(avg(avg_pm25) OVER w, 1) END AS pm25_7d_rolling
FROM daily_city_summary
WINDOW w AS (PARTITION BY city, state ORDER BY summary_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
ORDER BY city, state, summary_date;


-- 3. How did monthly PM2.5 change month over month?
--    Concept: LAG() over monthly aggregates; NULLIF to avoid division by zero.
WITH monthly AS (
    SELECT city, state, date_trunc('month', summary_date)::date AS month,
           avg(avg_pm25) AS pm25, count(*) AS days
    FROM daily_city_summary
    GROUP BY city, state, date_trunc('month', summary_date)
    HAVING count(*) >= 15                          -- require a reasonably complete month
)
SELECT city, state, month, round(pm25, 1) AS pm25,
       round(LAG(pm25) OVER w, 1)                                   AS prev_month_pm25,
       round(100 * (pm25 - LAG(pm25) OVER w) / NULLIF(LAG(pm25) OVER w, 0), 1) AS mom_change_pct
FROM monthly
WINDOW w AS (PARTITION BY city, state ORDER BY month)
ORDER BY city, state, month;


-- 4. At what hour of day (IST) is PM2.5 highest in each city?
--    Concept: date functions (extract hour in a time zone) + RANK().
WITH by_hour AS (
    SELECT l.city, l.state,
           extract(hour FROM f.reading_time AT TIME ZONE 'Asia/Kolkata')::int AS hour_ist,
           avg(f.pm25) AS pm25, count(*) AS n
    FROM fact_pollutant_hourly f
    JOIN dim_location l USING (location_id)
    WHERE l.city IS NOT NULL AND f.pm25 IS NOT NULL
    GROUP BY l.city, l.state, hour_ist
    HAVING count(*) >= 10                         -- no "peak hour" claims from a couple of readings
)
SELECT city, state, hour_ist, round(pm25, 1) AS avg_pm25, n
FROM (SELECT *, RANK() OVER (PARTITION BY city, state ORDER BY pm25 DESC) AS rnk FROM by_hour) r
WHERE rnk = 1
ORDER BY avg_pm25 DESC;


-- 5. How does PM2.5 around Diwali (+/- 3 days) compare with the two weeks before?
--    Concept: CTEs + dim_date festival flags + conditional aggregation.
WITH diwali AS (
    SELECT min(date) AS d FROM dim_date WHERE festival_name = 'Diwali' GROUP BY year
),
tagged AS (
    SELECT s.city, s.state, extract(year FROM dw.d)::int AS year, s.avg_pm25,
           CASE WHEN s.summary_date BETWEEN dw.d - 3 AND dw.d + 3 THEN 'diwali_window'
                WHEN s.summary_date BETWEEN dw.d - 17 AND dw.d - 4 THEN 'two_weeks_before' END AS period
    FROM daily_city_summary s
    JOIN diwali dw ON s.summary_date BETWEEN dw.d - 17 AND dw.d + 3
)
SELECT city, state, year,
       round(avg(avg_pm25) FILTER (WHERE period = 'two_weeks_before'), 1) AS pm25_before,
       round(avg(avg_pm25) FILTER (WHERE period = 'diwali_window'), 1)    AS pm25_diwali,
       round(100 * (avg(avg_pm25) FILTER (WHERE period = 'diwali_window')
                  / NULLIF(avg(avg_pm25) FILTER (WHERE period = 'two_weeks_before'), 0) - 1), 1) AS change_pct
FROM tagged
GROUP BY city, state, year
HAVING count(*) FILTER (WHERE period = 'diwali_window') >= 4
   AND count(*) FILTER (WHERE period = 'two_weeks_before') >= 7
ORDER BY year, change_pct DESC;


-- 6. How much worse is winter than monsoon, per city?
--    Concept: conditional aggregation with FILTER on a joined dimension.
SELECT s.city, s.state,
       round(avg(s.avg_pm25) FILTER (WHERE d.season = 'Winter'), 1)  AS winter_pm25,
       round(avg(s.avg_pm25) FILTER (WHERE d.season = 'Monsoon'), 1) AS monsoon_pm25,
       round(avg(s.avg_pm25) FILTER (WHERE d.season = 'Winter')
           / NULLIF(avg(s.avg_pm25) FILTER (WHERE d.season = 'Monsoon'), 0), 2) AS winter_to_monsoon_ratio
FROM daily_city_summary s
JOIN dim_date d ON d.date = s.summary_date
GROUP BY s.city, s.state
HAVING count(*) FILTER (WHERE d.season = 'Winter') >= 30
   AND count(*) FILTER (WHERE d.season = 'Monsoon') >= 30
ORDER BY winter_to_monsoon_ratio DESC;


-- 7. On what share of days is each city Poor or worse (median station AQI >= 201)?
--    Concept: boolean aggregation as a ratio; excluding days with no valid AQI.
SELECT city, state,
       count(*) FILTER (WHERE median_station_aqi IS NOT NULL)                    AS days_with_aqi,
       round(100.0 * count(*) FILTER (WHERE median_station_aqi >= 201)
             / NULLIF(count(*) FILTER (WHERE median_station_aqi IS NOT NULL), 0), 1) AS pct_days_poor_or_worse
FROM daily_city_summary
GROUP BY city, state
HAVING count(*) FILTER (WHERE median_station_aqi IS NOT NULL) >= 30
ORDER BY pct_days_poor_or_worse DESC;


-- 8. Which cities had the largest year-over-year change in PM2.5?
--    Concept: yearly aggregate + LAG over years + ranking by absolute change.
WITH yearly AS (
    SELECT city, state, extract(year FROM summary_date)::int AS year,
           avg(avg_pm25) AS pm25, count(*) AS days
    FROM daily_city_summary
    GROUP BY city, state, year
    HAVING count(*) >= 200                          -- mostly complete years only
),
yoy AS (
    SELECT *, LAG(pm25) OVER (PARTITION BY city, state ORDER BY year) AS prev_pm25,
              LAG(year) OVER (PARTITION BY city, state ORDER BY year) AS prev_year
    FROM yearly
)
SELECT city, state, prev_year, year, round(prev_pm25, 1) AS prev_pm25, round(pm25, 1) AS pm25,
       round(100 * (pm25 - prev_pm25) / NULLIF(prev_pm25, 0), 1) AS yoy_change_pct
FROM yoy
WHERE prev_year = year - 1
ORDER BY abs(pm25 - prev_pm25) DESC
LIMIT 20;


-- 9. How complete is the hourly data per city over the last 7 days?
--    Concept: generate_series to build the expected grid, LEFT JOIN to find gaps.
WITH hours AS (
    SELECT generate_series(date_trunc('hour', now() - interval '7 days') + interval '30 minutes',
                           now(), interval '1 hour') AS h
),
expected AS (
    SELECT l.location_id, l.city, l.state, hrs.h
    FROM dim_location l CROSS JOIN hours hrs
    WHERE l.is_active AND l.city IS NOT NULL
)
SELECT e.city, e.state,
       count(DISTINCT e.location_id)                                     AS active_stations,
       count(*)                                                          AS expected_station_hours,
       count(f.location_id)                                              AS stored_station_hours,
       round(100.0 * count(f.location_id) / count(*), 1)                 AS completeness_pct,
       round(100.0 * count(f.pm25) / count(*), 1)                        AS pm25_completeness_pct
FROM expected e
LEFT JOIN fact_pollutant_hourly f ON f.location_id = e.location_id AND f.reading_time = e.h
GROUP BY e.city, e.state
ORDER BY completeness_pct, e.city;


-- 10. Which stations are stale, since when, and is it a pattern by provider?
--     Concept: LEFT JOIN anti-pattern for "no recent data" + GROUPING SETS for subtotals.
WITH last_seen AS (
    SELECT l.location_id, l.location_name, l.provider, l.city, l.state,
           max(f.reading_time) AS last_hour
    FROM dim_location l
    LEFT JOIN fact_pollutant_hourly f USING (location_id)
    WHERE l.is_active
    GROUP BY l.location_id, l.location_name, l.provider, l.city, l.state
)
SELECT coalesce(provider, '(all providers)') AS provider,
       coalesce(state, '(all states)')       AS state,
       count(*)                                                                     AS active_stations,
       count(*) FILTER (WHERE last_hour IS NULL
                           OR last_hour < now() - make_interval(hours => fn_setting('fresh_hours')::int)) AS stale_stations,
       min(last_hour) AT TIME ZONE 'Asia/Kolkata'                                   AS oldest_last_hour_ist
FROM last_seen
GROUP BY GROUPING SETS ((provider), (provider, state), ())
ORDER BY provider NULLS FIRST, state NULLS FIRST;
