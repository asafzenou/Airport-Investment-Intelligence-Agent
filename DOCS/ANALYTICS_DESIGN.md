# Analytics Design

## 1. Purpose

This document defines the deterministic analytics layer used by the Airport
Investment Intelligence Agent.

The analytics layer converts the raw aviation tables into small, explainable
result tables. The LLM does not calculate scores. It reads the stored results,
explains them, and answers conversational follow-up questions.

The design intentionally stays simple:

- SQLite performs the aggregations.
- Python applies the formulas and writes the results.
- Every metric is reproducible from stored source data.
- Each result includes its data period, scope, and limitations.
- No ORM, vector database, or additional analytics framework is required.

## 2. Source Tables

The calculations use the existing pipeline tables:

| Table | Purpose |
| --- | --- |
| `airports` | Airport code, name, state, region, latitude, and longitude |
| `airport_traffic` | Monthly passengers, seats, and flight activity by airport |
| `routes` | Origin-destination activity, flight count, and route distance |
| `airport_operations` | Monthly scheduled flights, delays, cancellations, and delay minutes |

The exact source column names may follow the repository schema. The formulas in
this document describe the required values independent of naming differences.

## 3. Shared Rules

### 3.1 Latest complete data

Each calculation uses the latest complete period available in its required
source tables. It must not assume that all sources end in the same month.

For a calculation that joins multiple sources:

```
analysis_end_month = minimum(latest month available in each required source)
```

The selected month is stored with the result.

### 3.2 Rolling windows

When enough history exists:

```
current_12_months  = analysis_end_month and the previous 11 months
previous_12_months = the 12 months immediately before current_12_months
```

Growth calculations require two complete and comparable 12-month windows. If
24 months are unavailable, growth is `NULL`; the system does not silently
compare unequal windows.

Point-in-time operational comparisons use the latest common month unless a
different period is explicitly requested by the user.

### 3.3 Percentage change

```
growth_percent =
    ((current_total - previous_total) / previous_total) * 100
```

If `previous_total` is zero or missing, `growth_percent` is `NULL`.

### 3.4 Weighted load factor

Load factor must be calculated from totals, not by averaging monthly
percentages:

```
weighted_load_factor_percent =
    (sum(passengers) / sum(seats)) * 100
```

If total seats are zero or missing, the result is `NULL`.

### 3.5 Operational rates

```
delay_rate_percent =
    (delayed_departures / scheduled_departures) * 100

cancellation_rate_percent =
    (cancelled_departures / scheduled_departures) * 100

average_delay_minutes =
    total_departure_delay_minutes / delayed_departures
```

`delayed_departures` means departures with a delay greater than 15 minutes, as
defined by the pipeline. A denominator of zero produces `NULL`.

### 3.6 Min-max normalization

The New England ranking compares metrics with different units. Each eligible
airport is normalized against the candidate set:

```
normalized_value =
    (value - minimum_value) / (maximum_value - minimum_value)
```

The result is between `0` and `1`. If every airport has the same value, the
normalized value is `0.5`. Missing metrics are not converted to zero.

### 3.7 Rounding

- Percentages: two decimal places.
- Scores: one decimal place on a `0-100` scale.
- Counts and capacity estimates: whole numbers.
- Full-precision values are used during calculations; rounding happens only
  when the result is written.

## 4. Analytics Result Tables

The analytics layer creates four small derived tables:

1. `analytics_expansion_scores`
2. `analytics_congestion`
3. `analytics_long_haul`
4. `analytics_unmet_demand`

These are replaceable derived results, not historical source-of-truth tables.
Every analytics run performs the following in one transaction:

```
DELETE FROM analytics_expansion_scores;
DELETE FROM analytics_congestion;
DELETE FROM analytics_long_haul;
DELETE FROM analytics_unmet_demand;
```

The newly calculated rows are then inserted. The transaction is committed only
after all four calculations succeed. On failure, it is rolled back so the
previous complete analytics snapshot remains available.

All tables include:

- `calculated_at`: UTC timestamp of the analytics run.
- `period_start`: first month used by the calculation.
- `period_end`: last month used by the calculation.
- `data_scope`: short description of what the underlying data includes.
- `limitations`: calculation-specific caveats.

## 5. New England Terminal Expansion Ranking

### 5.1 Goal

Rank New England airports by evidence of growing demand, high seat utilization,
and operational pressure. The result is an investment-screening indicator, not
a claim that a terminal project is financially viable by itself.

New England states:

```
CT, ME, MA, NH, RI, VT
```

### 5.2 Eligibility

An airport is eligible when it:

- is located in a New England state;
- has passenger and seat data for the current 12-month window;
- has at least `100,000` passengers in the current 12-month window.

The passenger threshold prevents very small airports with volatile percentage
changes from dominating the ranking. It is a documented project assumption and
can be configured.

### 5.3 Input metrics

For each eligible airport:

```
passenger_growth_percent =
    ((current_12m_passengers - previous_12m_passengers)
    / previous_12m_passengers) * 100

load_factor_percent =
    (current_12m_passengers / current_12m_seats) * 100

delay_rate_percent =
    (delayed_departures / scheduled_departures) * 100

current_passengers = current_12m_passengers
```

Only positive growth contributes to expansion pressure:

```
positive_growth = max(passenger_growth_percent, 0)
```

### 5.4 Expansion score

Normalize the four values across eligible New England airports, then calculate:

```
expansion_score = 100 * (
    0.40 * normalized_positive_growth
  + 0.30 * normalized_load_factor
  + 0.20 * normalized_delay_rate
  + 0.10 * normalized_current_passengers
)
```

Weights:

| Metric | Weight | Meaning |
| --- | ---: | --- |
| Passenger growth | 40% | Demand is increasing |
| Load factor | 30% | Existing seat capacity is highly utilized |
| Delay rate | 20% | Current operations show pressure |
| Passenger volume | 10% | The opportunity has meaningful scale |

Airports are ordered by `expansion_score DESC`, then by airport code for a
stable tie-break.

If the growth metric is unavailable, the row remains visible but
`expansion_score` is `NULL` and `is_rankable` is false. The missing history is
reported rather than replaced with an assumed value.

### 5.5 Table schema

`analytics_expansion_scores` contains:

| Column | Description |
| --- | --- |
| `airport_code` | Airport IATA code |
| `airport_name` | Airport name |
| `state_code` | New England state code |
| `current_passengers` | Passengers in the current window |
| `previous_passengers` | Passengers in the comparison window |
| `passenger_growth_percent` | Year-over-year rolling-window growth |
| `load_factor_percent` | Weighted seat utilization |
| `delay_rate_percent` | Share of departures delayed over 15 minutes |
| `expansion_score` | Weighted deterministic score, `0-100` |
| `rank_position` | Rank among eligible airports |
| `is_rankable` | Whether all required score inputs exist |
| `calculated_at` | Calculation timestamp |
| `period_start` | Start of current window |
| `period_end` | End of current window |
| `data_scope` | Scope statement |
| `limitations` | Missing data and interpretation caveats |

## 6. LAX vs. SNA Congestion Comparison

### 6.1 Goal

Compare congestion at Los Angeles International Airport (`LAX`) and John Wayne
Airport (`SNA`) over the same operational period.

### 6.2 Metrics

For each airport:

```
delay_rate_percent =
    (delayed_departures / scheduled_departures) * 100

cancellation_rate_percent =
    (cancelled_departures / scheduled_departures) * 100

average_delay_minutes =
    total_departure_delay_minutes / delayed_departures
```

### 6.3 Congestion index

To combine the metrics without comparing incompatible raw units, each value is
converted to a capped pressure score. The caps are transparent project
assumptions, not official BTS thresholds:

```
delay_pressure = min(delay_rate_percent / 40, 1) * 100

cancellation_pressure = min(cancellation_rate_percent / 10, 1) * 100

delay_severity_pressure = min(average_delay_minutes / 60, 1) * 100
```

```
congestion_index =
    0.50 * delay_pressure
  + 0.20 * cancellation_pressure
  + 0.30 * delay_severity_pressure
```

The index is between `0` and `100`. It measures operational congestion only; it
does not measure terminal crowding, security queues, gate availability, or
road traffic.

The comparison uses the same start and end months for both airports. The higher
index is labelled `more_congested`.

### 6.4 Table schema

`analytics_congestion` contains one row per compared airport:

| Column | Description |
| --- | --- |
| `comparison_key` | Stable group name, for example `LAX_SNA` |
| `airport_code` | `LAX` or `SNA` |
| `scheduled_departures` | Total scheduled departures |
| `delayed_departures` | Departures delayed over 15 minutes |
| `cancelled_departures` | Cancelled departures |
| `delay_rate_percent` | Delay frequency |
| `cancellation_rate_percent` | Cancellation frequency |
| `average_delay_minutes` | Average severity among delayed flights |
| `congestion_index` | Deterministic index, `0-100` |
| `comparison_result` | `more_congested` or `less_congested` |
| `calculated_at` | Calculation timestamp |
| `period_start` | Comparison period start |
| `period_end` | Comparison period end |
| `data_scope` | Scope statement |
| `limitations` | Interpretation caveats |

## 7. Anchorage Long-Haul Flight Percentage

### 7.1 Goal

Calculate the percentage of departing flights from Ted Stevens Anchorage
International Airport (`ANC`) that are long-haul.

### 7.2 Definition

A departing flight is considered long-haul when:

```
route_distance_miles >= LONG_HAUL_MILES
```

`LONG_HAUL_MILES` is configured as `2,500` miles. This is a documented project
assumption, not an official BTS long-haul classification.

### 7.3 Formula

The calculation is weighted by flight count. It does not calculate the share of
distinct destinations:

```
long_haul_flights =
    sum(flight_count where route_distance_miles >= LONG_HAUL_MILES)

total_departing_flights = sum(flight_count for all ANC origin routes)

long_haul_percentage =
    (long_haul_flights / total_departing_flights) * 100
```

If total departing flights are zero, the percentage is `NULL`.

The current routes source represents US domestic reported flights. Therefore,
the result is explicitly labelled as a domestic-data estimate and may omit
international long-haul departures from ANC.

### 7.4 Table schema

`analytics_long_haul` contains:

| Column | Description |
| --- | --- |
| `airport_code` | `ANC` |
| `long_haul_threshold_miles` | Configured threshold |
| `long_haul_flights` | Flights at or above the threshold |
| `total_departing_flights` | All counted departures from ANC |
| `long_haul_percentage` | Flight-weighted percentage |
| `calculated_at` | Calculation timestamp |
| `period_start` | Route period start |
| `period_end` | Route period end |
| `data_scope` | Domestic departing flights represented by the source |
| `limitations` | International-flight and threshold caveats |

## 8. SFO Unmet Demand Analysis

### 8.1 Goal

Estimate whether San Francisco International Airport (`SFO`) shows signs of
capacity pressure and explain the measurable reasons.

Public BTS data describes served passengers and scheduled capacity. It does not
contain failed booking attempts, rejected passengers, willingness to pay, or
airline schedule requests. Therefore, the system cannot measure true unmet
demand. It reports an `unmet_demand_proxy`, not a factual count of unserved
passengers.

### 8.2 Capacity-gap proxy

The target sustainable load factor is configured as:

```
TARGET_LOAD_FACTOR = 0.85
```

This is a project assumption. It represents a buffer below completely full
scheduled capacity.

```
current_load_factor = current_passengers / current_seats

estimated_additional_seats_needed =
    max(0, (current_passengers / TARGET_LOAD_FACTOR) - current_seats)

unmet_passenger_capacity_proxy =
    max(0, current_passengers - (current_seats * TARGET_LOAD_FACTOR))
```

The first value estimates how many additional scheduled seats would be required
to carry the observed passengers at the target load factor. The second expresses
the same pressure in passenger-capacity terms. Neither value represents actual
passengers who were denied travel.

### 8.3 Supporting indicators

```
passenger_growth_percent =
    ((current_12m_passengers - previous_12m_passengers)
    / previous_12m_passengers) * 100

delay_rate_percent =
    (delayed_departures / scheduled_departures) * 100

cancellation_rate_percent =
    (cancelled_departures / scheduled_departures) * 100
```

### 8.4 Deterministic reason flags

The result stores reasons generated from fixed rules:

| Flag | Rule | Meaning |
| --- | --- | --- |
| `HIGH_LOAD_FACTOR` | `load_factor_percent >= 85` | Scheduled seat capacity is highly utilized |
| `POSITIVE_PASSENGER_GROWTH` | `passenger_growth_percent > 0` | Passenger demand increased year over year |
| `HIGH_DELAY_RATE` | `delay_rate_percent >= 20` | At least one in five departures is delayed over 15 minutes |
| `ELEVATED_CANCELLATIONS` | `cancellation_rate_percent >= 2` | Cancellations add operational friction |
| `INSUFFICIENT_HISTORY` | Comparable 12-month windows are unavailable | Growth cannot be calculated reliably |

These thresholds are transparent project assumptions. The explanation must name
the triggered flags and their underlying values.

### 8.5 Demand-pressure score

The score summarizes the evidence but does not replace the raw metrics.

```
load_pressure =
    min(max((load_factor_percent - 70) / (95 - 70), 0), 1) * 100

growth_pressure =
    min(max(passenger_growth_percent / 20, 0), 1) * 100

delay_pressure =
    min(delay_rate_percent / 40, 1) * 100

cancellation_pressure =
    min(cancellation_rate_percent / 10, 1) * 100
```

```
unmet_demand_score =
    0.45 * load_pressure
  + 0.25 * growth_pressure
  + 0.20 * delay_pressure
  + 0.10 * cancellation_pressure
```

The score is between `0` and `100`. If passenger growth is unavailable, the
score is `NULL`; the system returns the available raw metrics and the
`INSUFFICIENT_HISTORY` flag instead of reweighting the formula.

### 8.6 Table schema

`analytics_unmet_demand` contains:

| Column | Description |
| --- | --- |
| `airport_code` | `SFO` |
| `current_passengers` | Passengers in the current window |
| `current_seats` | Scheduled seats in the current window |
| `load_factor_percent` | Weighted seat utilization |
| `passenger_growth_percent` | Comparable rolling-window growth |
| `delay_rate_percent` | Operational delay frequency |
| `cancellation_rate_percent` | Cancellation frequency |
| `target_load_factor_percent` | Configured target, normally `85%` |
| `estimated_additional_seats_needed` | Seat-capacity pressure estimate |
| `unmet_passenger_capacity_proxy` | Passenger-capacity pressure estimate |
| `unmet_demand_score` | Deterministic score, `0-100` |
| `reason_flags` | Comma-separated deterministic reason codes |
| `calculated_at` | Calculation timestamp |
| `period_start` | Current window start |
| `period_end` | Current window end |
| `data_scope` | Served traffic and scheduled-capacity scope |
| `limitations` | Explicit statement that true unmet demand is unavailable |

## 9. Analytics Execution Flow

The implementation exposes one simple analytics service with four public
methods and one coordinator:

```
calculate_expansion_scores()
calculate_congestion()
calculate_long_haul()
calculate_unmet_demand()
run_all()
```

`run_all()` performs:

1. Read the required source data through `AviationDAL`.
2. Validate that required fields and denominators are present.
3. Calculate all four result sets in memory.
4. Open one SQLite transaction.
5. Delete existing rows from the four analytics tables.
6. Insert the newly calculated rows.
7. Commit the transaction.
8. Roll back on any failure and log the error.

The analytics run does not download data. Data collection remains the
responsibility of the existing ETL pipeline.

## 10. AI Usage

The LLM is used only after deterministic analytics are complete. It may:

- select the relevant analytics query based on the user's question;
- retrieve the stored result rows;
- explain scores, metrics, assumptions, uncertainty, and limitations;
- preserve context for follow-up questions;
- compare already-calculated values in natural language.

The LLM must not invent missing values, modify formulas, or produce an
unsupported ranking. If required data is missing, it must explain what is
missing and return the available evidence.

## 11. Main Limitations

- The scores identify indicators of capacity pressure, not project ROI.
- Construction cost, terminal condition, land availability, gate constraints,
  local regulation, airline commitments, and financing are outside the data
  scope.
- Delay data measures flight operations, not terminal crowding directly.
- The ANC long-haul result is limited by the domestic scope of the routes source.
- SFO true unmet demand cannot be observed; only a transparent capacity-pressure
  proxy is calculated.
- Results are only as current and complete as the latest successful ETL runs.

These limitations must be returned with relevant answers rather than hidden by
the conversational layer.
